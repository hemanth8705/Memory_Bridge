"""Actian VectorAI DB - the semantic memory layer.

Two layers sit on top of Mongo, and this module is both of them:

  WRITE  index_memories()    pipeline, once extraction finishes: every memory
                             row and every conversation summary is embedded and
                             upserted here alongside the Mongo write.
  READ   search_memories()   retrieval: the context handed to Gemini is the
                             semantically nearest material to what was actually
                             asked, not simply the newest N rows.

Mongo stays the system of record. This is an index over it, and every call here
degrades instead of raising: if the container is down, the key is missing, or a
search errors, the caller falls back to the Mongo path and the user still gets
real context. That is the point of `available` / `last_error` - a failure here
must never cost the user an answer while their phone is ringing.

Point IDs are deterministic UUID5s derived from the Mongo _id (memories) or the
recording hash (conversations), so re-processing a recording overwrites its
points rather than accumulating duplicates.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .config import get_settings
from .embeddings import EmbeddingError, embed_query, embed_texts

log = logging.getLogger("memorybridge.vectordb")

# Namespace for deterministic point IDs. Fixed forever - changing it orphans
# every point already stored.
_ID_NAMESPACE = uuid.UUID("6f1a4b2e-9c3d-5e7f-8a1b-2c3d4e5f6a7b")

MEMORY = "memory"
CONVERSATION = "conversation"

_client: Any = None
_available: bool = False
_last_error: Optional[str] = None
_collection_ready: bool = False


# --- lifecycle --------------------------------------------------------------

def point_id(kind: str, key: str) -> str:
    """Stable UUID for one indexed item, so upserts are idempotent."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{kind}:{key}"))


async def connect() -> None:
    """Open the gRPC channel and ensure the collection exists.

    Never raises: a missing vector DB degrades the app, it does not stop it.
    """
    global _client, _available, _last_error, _collection_ready
    settings = get_settings()
    _last_error = None
    _collection_ready = False

    if not settings.vector_enabled:
        _available = False
        _last_error = "disabled (VECTOR_DB_ENABLED=false)"
        log.info("VectorAI DB disabled by configuration")
        return

    try:
        from actian_vectorai import AsyncVectorAIClient, Distance, VectorParams

        client = AsyncVectorAIClient(
            settings.vector_url,
            api_key=settings.vector_api_key or None,
            timeout=settings.vector_timeout,
        )
        await client.connect()
        info = await client.health_check()

        await client.collections.get_or_create(
            settings.vector_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dim, distance=Distance.Cosine,
            ),
        )
        _client = client
        _available = True
        _collection_ready = True
        log.info(
            "VectorAI DB connected (%s, collection=%s, dim=%d): %s",
            settings.vector_url, settings.vector_collection,
            settings.embedding_dim, info.get("version", "unknown version"),
        )
    except Exception as exc:
        _client = None
        _available = False
        _last_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "VectorAI DB unavailable at %s (%s). Falling back to MongoDB-only "
            "retrieval.", settings.vector_url, _last_error,
        )


async def close() -> None:
    global _client, _available, _collection_ready
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
    _client, _available, _collection_ready = None, False, False


def is_available() -> bool:
    return _available and _client is not None


async def status() -> dict:
    """Live status for /health - reports what the DB actually holds."""
    settings = get_settings()
    payload: dict[str, Any] = {
        "enabled": settings.vector_enabled,
        "url": settings.vector_url,
        "collection": settings.vector_collection,
        "embedding_model": settings.embedding_model,
        "dimension": settings.embedding_dim,
        "connected": is_available(),
        "collection_ready": _collection_ready,
        "error": _last_error,
    }
    if not is_available():
        return payload
    try:
        payload["indexed_points"] = await _client.points.count(settings.vector_collection)
        info = await _client.health_check()
        payload["server_version"] = info.get("version")
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


# --- write layer ------------------------------------------------------------

def _stamp(value: Any) -> tuple[Optional[str], int]:
    """A datetime as (ISO string, epoch seconds) for payload storage.

    Payload values must be scalars, so the ISO string is what gets read back
    and the epoch int is what a numeric range filter can compare on.
    """
    if not isinstance(value, datetime):
        return None, 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(), int(value.timestamp())


def _memory_embedding_text(row: dict, contact_name: str) -> str:
    """What actually gets embedded for a memory row.

    The bare content is already a standalone sentence, but prefixing who it is
    about and what kind of memory it is measurably sharpens retrieval - "what
    did I promise Priya?" needs both the name and the word "promise" in the
    embedded text to beat every other Priya memory.
    """
    kind = (row.get("type") or "other").replace("_", " ")
    who = contact_name or "an unknown caller"
    return f"About {who} ({kind}): {row.get('content', '')}"


def _conversation_embedding_text(summary: str, topics: Sequence[str],
                                 contact_name: str, when: Optional[str]) -> str:
    who = contact_name or "an unknown caller"
    head = f"Conversation with {who}"
    if when:
        head = f"{head} on {when[:10]}"
    text = f"{head}: {summary}"
    if topics:
        text = f"{text} Topics: {', '.join(topics)}."
    return text


async def _forget_recording(source_recording: str) -> None:
    """Drop every point that came from one recording. Best effort."""
    from actian_vectorai import Field, FilterBuilder

    settings = get_settings()
    try:
        await _client.points.delete(
            settings.vector_collection,
            filter=FilterBuilder().must(
                Field("source_recording").eq(source_recording)
            ).build(),
        )
    except Exception as exc:
        # Worst case we leave stale points behind; the upsert below still
        # writes the current ones, so retrieval stays correct-but-noisy.
        log.debug("Could not clear old points for %s: %s", source_recording, exc)


async def index_memories(
    *,
    memories: Sequence[dict],
    contact_id: Optional[str],
    contact_name: str,
    phone_key: Optional[str],
    phone_number: Optional[str],
    recorded_at: Optional[datetime],
    summary: str = "",
    topics: Optional[Sequence[str]] = None,
    source_recording: Optional[str] = None,
    source_transcript_id: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Embed and upsert one recording's memories + its conversation summary.

    `memories` are rows that already carry their Mongo `_id`, which becomes the
    point ID. Returns a small report the pipeline records on the job, so the
    indexing outcome is visible without reading logs.
    """
    report = {"indexed": 0, "skipped": True, "error": None}
    settings = get_settings()

    if not is_available():
        report["error"] = _last_error or "vector db unavailable"
        return report

    recorded_iso, recorded_epoch = _stamp(recorded_at)
    created_iso, created_epoch = _stamp(datetime.now(timezone.utc))

    base = {
        "contact_id": contact_id or "",
        "contact_name": contact_name or "",
        "phone_key": phone_key or "",
        "phone_number": phone_number or "",
        "source_recording": source_recording or "",
        "source_transcript_id": source_transcript_id or "",
        "recorded_at": recorded_iso or created_iso or "",
        "recorded_epoch": recorded_epoch or created_epoch,
        "created_at": created_iso or "",
    }

    texts: list[str] = []
    payloads: list[dict] = []
    ids: list[str] = []

    for row in memories:
        content = (row.get("content") or "").strip()
        mongo_id = row.get("_id")
        if not content or mongo_id is None:
            continue
        row_created_iso, _ = _stamp(row.get("created_at"))
        texts.append(_memory_embedding_text(row, contact_name))
        payloads.append({
            **base,
            "kind": MEMORY,
            "type": row.get("type") or "other",
            "content": content,
            "confidence": row.get("confidence") or "medium",
            "owed_by": row.get("owed_by") or "",
            # The memory's own Mongo timestamp, so a retrieved row dates
            # itself exactly as the Mongo path would.
            "created_at": row_created_iso or created_iso or "",
            "mongo_id": str(mongo_id),
        })
        ids.append(point_id(MEMORY, str(mongo_id)))

    summary = (summary or "").strip()
    if summary and source_recording:
        texts.append(_conversation_embedding_text(
            summary, topics or [], contact_name, recorded_iso,
        ))
        payloads.append({
            **base,
            "kind": CONVERSATION,
            "type": CONVERSATION,
            "content": summary,
            "confidence": "high",
            "owed_by": "",
            "topics": ", ".join(topics or []),
            "mongo_id": source_recording,
        })
        ids.append(point_id(CONVERSATION, source_recording))

    if not texts:
        report["skipped"] = False
        return report

    try:
        from actian_vectorai import PointStruct

        # A retry re-inserts the memory rows under fresh Mongo _ids, so the
        # deterministic point IDs move too and the old points would linger as
        # duplicates. Clear this recording's slice first, so re-processing
        # converges instead of accumulating.
        if source_recording:
            await _forget_recording(source_recording)

        vectors = await asyncio.to_thread(embed_texts, texts, api_key)
        points = [
            PointStruct(id=pid, vector=vector, payload=payload)
            for pid, vector, payload in zip(ids, vectors, payloads)
        ]
        await _client.points.upsert(settings.vector_collection, points)
    except EmbeddingError as exc:
        report["error"] = str(exc)
        log.warning("Embedding failed while indexing %s: %s", contact_name, exc)
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("VectorAI upsert failed for %s: %s", contact_name, exc)
        return report

    report.update({"indexed": len(points), "skipped": False})
    log.info("Indexed %d points into VectorAI DB for %s", len(points), contact_name)
    return report


# --- read layer -------------------------------------------------------------

# The server applies a payload filter AFTER taking the top-`limit` candidates by
# vector similarity - it post-filters, it does not filter during the search.
# Measured against 1.0.2: a search scoped to a contact holding 13 of the
# collection's 31 points returns 0 rows at limit=3, 1 at limit=10, and all 13
# only at limit=31. Asked naively, per-contact retrieval would therefore return
# nothing whenever another contact's memories happened to rank higher globally -
# silently, as an empty result rather than an error.
#
# So a scoped search over-fetches: ask for a window large enough to contain
# every point that could match, then trim to `limit` here. The floor covers any
# realistic personal memory store outright (one point per memory plus one per
# conversation), which makes the scan exhaustive and the result exact.
_FETCH_FLOOR = 512
_FETCH_FACTOR = 20
_FETCH_CEILING = 4096


def _fetch_window(limit: int) -> int:
    return min(max(limit * _FETCH_FACTOR, _FETCH_FLOOR), _FETCH_CEILING)


def _owner_filter(contact_id: Optional[str], phone_key: Optional[str]):
    """Restrict a search to one person.

    phone_key and contact_id are OR'd for the same reason the Mongo queries OR
    them: the phone number is the real identifier, but rows written before a
    contact was resolved are only reachable through contact_id.
    """
    from actian_vectorai import Field, FilterBuilder

    builder = FilterBuilder()
    clauses = 0
    if phone_key:
        builder = builder.should(Field("phone_key").eq(phone_key))
        clauses += 1
    if contact_id:
        builder = builder.should(Field("contact_id").eq(contact_id))
        clauses += 1
    if clauses == 0:
        return None
    return builder.build()


async def search_memories(
    query: str,
    *,
    contact_id: Optional[str] = None,
    phone_key: Optional[str] = None,
    limit: int = 24,
    api_key: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> tuple[list[dict], Optional[str]]:
    """Semantic retrieval: the context for one question about one person.

    Returns (rows, error). `rows` are payloads with a `score`, most relevant
    first. An empty list with no error means the person has nothing indexed
    yet; an error means the caller should fall back to Mongo.
    """
    settings = get_settings()
    if not is_available():
        return [], _last_error or "vector db unavailable"

    owner = _owner_filter(contact_id, phone_key)
    if owner is None:
        # No owner to scope to. Answering from *everyone's* memories would be
        # worse than answering from none, so refuse rather than leak.
        return [], "no contact_id or phone_key to scope the search to"

    try:
        vector = await asyncio.to_thread(embed_query, query, api_key)
        results = await _client.points.search(
            settings.vector_collection,
            vector=vector,
            # Over-fetch, then trim - see _fetch_window. Passing `limit`
            # straight through here is the bug that quietly empties results.
            limit=_fetch_window(limit),
            filter=owner,
            with_payload=True,
            score_threshold=score_threshold,
        )
    except EmbeddingError as exc:
        return [], str(exc)
    except Exception as exc:
        log.warning("VectorAI search failed: %s", exc)
        return [], f"{type(exc).__name__}: {exc}"

    if len(results) >= _fetch_window(limit):
        # Every candidate in the window matched, so the window was the binding
        # constraint and relevant memories may lie beyond it. Only reachable
        # with a collection far larger than one person's call history.
        log.warning(
            "Scoped search saturated the %d-point fetch window; results may be "
            "incomplete. Raise _FETCH_CEILING or shard the collection.",
            _fetch_window(limit),
        )

    rows: list[dict] = []
    for scored in results[:limit]:
        payload = dict(scored.payload or {})
        payload["score"] = round(float(scored.score), 4)
        payload["point_id"] = str(scored.id)
        rows.append(payload)
    return rows, None


async def count_for(contact_id: Optional[str] = None,
                    phone_key: Optional[str] = None) -> Optional[int]:
    """How many points are indexed, optionally for one person. None if down."""
    if not is_available():
        return None
    settings = get_settings()
    try:
        return await _client.points.count(
            settings.vector_collection,
            filter=_owner_filter(contact_id, phone_key),
        )
    except Exception:
        return None
