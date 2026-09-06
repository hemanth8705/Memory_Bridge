"""Contacts, their memories, and question answering over them."""
import asyncio
import logging
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import contacts as contacts_mod, llm, vectordb
from ..config import get_settings
from ..db import get_db

log = logging.getLogger("memorybridge.contacts")

router = APIRouter()


def _serialize_contact(doc: dict) -> dict:
    return {
        "contact_id": str(doc["_id"]),
        "name": doc.get("name") or "Unknown caller",
        "phone_number": doc.get("phone_number"),
        "conversation_count": doc.get("conversation_count", 0),
        "last_conversation_at": doc.get("last_conversation_at"),
    }


@router.get("/contacts")
async def list_contacts():
    db = get_db()
    out = []
    async for doc in db.contacts.find().sort("last_conversation_at", -1):
        out.append(_serialize_contact(doc))
    return {"contacts": out}


async def _load_contact(contact_id: str) -> dict:
    db = get_db()
    try:
        oid = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed contact_id")
    doc = await db.contacts.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    return doc


@router.get("/contacts/{contact_id}/memories")
async def contact_memories(contact_id: str):
    """Everything remembered about one person, grouped for the contact screen."""
    db = get_db()
    contact = await _load_contact(contact_id)

    memories = []
    async for doc in db.memories.find({"contact_id": contact_id}).sort("created_at", -1):
        doc["memory_id"] = str(doc.pop("_id"))
        memories.append(doc)

    conversations = []
    # Chronological order of the actual calls, not the order we processed
    # them in - those diverge whenever a backlog is scanned all at once.
    async for doc in db.recordings.find(
        {"contact_id": contact_id, "status": "completed"}
    ).sort("recorded_at", -1):
        conversations.append({
            "hash": doc.get("hash"),
            "filename": doc.get("filename"),
            "summary": doc.get("summary", ""),
            "topics": doc.get("topics", []),
            "recorded_at": doc.get("recorded_at"),
            "processed_at": doc.get("processed_at"),
        })

    topics: list[str] = []
    for conversation in conversations:
        for topic in conversation.get("topics") or []:
            if topic not in topics:
                topics.append(topic)

    grouped: dict[str, list[dict]] = {}
    for memory in memories:
        grouped.setdefault(memory.get("type", "other"), []).append(memory)

    return {
        **_serialize_contact(contact),
        "topics": topics,
        "memories": memories,
        "grouped": grouped,
        "conversations": conversations,
    }


class SearchRequest(BaseModel):
    query: str
    contact: Optional[str] = None
    contact_id: Optional[str] = None


def _memories_block(memories: list[dict], conversations: list[dict]) -> str:
    lines = []
    for conversation in conversations:
        when = conversation.get("recorded_at") or conversation.get("processed_at")
        stamp = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "unknown date"
        if conversation.get("summary"):
            lines.append(f"[conversation on {stamp}] {conversation['summary']}")
    for memory in memories:
        when = memory.get("created_at")
        stamp = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "unknown date"
        lines.append(f"[{memory.get('type', 'other')}, recorded {stamp}] {memory.get('content', '')}")
    return "\n".join(lines)


# --- retrieval: Actian VectorAI DB ------------------------------------------

def _vector_block(rows: list[dict]) -> str:
    """Vector-search hits -> the same text block the Mongo path produces.

    Deliberately identical in shape to _memories_block: the prompts in llm.py
    were written against that format, and swapping the *source* of the context
    should not quietly change what the model is reading.

    Order is relevance order, most similar first - that is the whole point of
    retrieving rather than listing, and the model weights what it sees first.
    """
    lines = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        if row.get("kind") == vectordb.CONVERSATION:
            stamp = (row.get("recorded_at") or "")[:10] or "unknown date"
            lines.append(f"[conversation on {stamp}] {content}")
        else:
            stamp = (row.get("created_at") or row.get("recorded_at") or "")[:10] or "unknown date"
            lines.append(f"[{row.get('type', 'other')}, recorded {stamp}] {content}")
    return "\n".join(lines)


async def retrieve_context(
    query: str,
    *,
    contact_id: Optional[str] = None,
    phone_key: Optional[str] = None,
    api_key: Optional[str] = None,
    limit: int = 24,
) -> tuple[list[dict], str, Optional[str], Optional[str]]:
    """Get the context for one question about one person from the vector DB.

    This is the read half of the semantic layer. It embeds the question, runs a
    similarity search scoped to that person, and returns the nearest memories -
    as opposed to the Mongo path, which returns the most *recent* ones and
    leaves the model to find the relevant part.

    Returns (rows, block, source, error). `source` is "vector" when the vector
    DB actually answered, and None when the caller must fall back to Mongo:
    the vector DB being down, the embedding key being unusable, or simply
    nothing indexed for this person yet. Never raises - the caller is often
    serving a popup while the phone is ringing.
    """
    rows, error = await vectordb.search_memories(
        query, contact_id=contact_id, phone_key=phone_key,
        limit=limit, api_key=api_key,
    )
    if error:
        log.info("Vector retrieval unavailable, falling back to MongoDB: %s", error)
        return [], "", None, error
    if not rows:
        # Nothing indexed for this person yet (memories written before the
        # vector layer existed, for instance). Mongo still has them.
        return [], "", None, "no_vector_matches"
    return rows, _vector_block(rows), "vector", None


@router.post("/memory/search")
async def search_memory(
    payload: SearchRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
):
    """Ask a question about a person; answered from their stored memories."""
    db = get_db()

    contact = None
    if payload.contact_id:
        contact = await _load_contact(payload.contact_id)
    elif payload.contact:
        contact = await db.contacts.find_one(
            {"name": {"$regex": payload.contact, "$options": "i"}}
        )
    if contact is None:
        raise HTTPException(
            status_code=404,
            detail="No matching contact. Pass contact_id, or a name that exists.",
        )

    contact_id = str(contact["_id"])
    name = contact.get("name") or "this person"

    # Retrieval, first choice: semantically nearest memories to the actual
    # question, from the Actian VectorAI DB.
    rows, block, source, retrieval_error = await retrieve_context(
        payload.query,
        contact_id=contact_id,
        phone_key=contact.get("phone_key"),
        api_key=x_gemini_api_key,
        limit=get_settings().vector_search_limit,
    )
    considered = len(rows)

    if source is None:
        # Fallback: the most recent rows straight out of Mongo. Less precise -
        # the model has to find the relevant part itself - but it always works.
        memories = []
        async for doc in db.memories.find({"contact_id": contact_id}).sort("created_at", -1).limit(200):
            doc.pop("_id", None)
            memories.append(doc)
        conversations = []
        async for doc in db.recordings.find(
            {"contact_id": contact_id, "status": "completed"}
        ).sort("recorded_at", -1).limit(50):
            conversations.append(doc)
        block = _memories_block(memories, conversations)
        source = "mongodb"
        considered = len(memories)

    try:
        result = await asyncio.to_thread(
            llm.answer_question, payload.query, name, block, x_gemini_api_key
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "contact_id": contact_id,
        "contact_name": name,
        "query": payload.query,
        "answer": result.get("answer", ""),
        "suggested_questions": result.get("suggested_questions", []),
        "memories_considered": considered,
        # Which layer actually supplied the context, so the retrieval path is
        # visible from the API rather than only in the logs.
        "retrieval": source,
        "retrieval_error": retrieval_error,
        "matches": [
            {"score": r.get("score"), "kind": r.get("kind"),
             "type": r.get("type"), "content": r.get("content")}
            for r in rows
        ],
    }


# --- incoming-call briefing -------------------------------------------------

class CallerLookupRequest(BaseModel):
    phone_number: str


# Read while the phone is ringing, so the most conversationally useful kinds
# of memory go in front of the model first.
_BRIEFING_PRIORITY = [
    "promise", "follow_up", "important_date", "health", "family",
    "job", "travel", "study", "event", "interest", "personal", "other",
]

# Enough to brief well, few enough to keep the Gemini call fast.
_BRIEFING_MEMORY_LIMIT = 40

# A briefing has no user question to embed, so it gets a standing one. It spells
# out the same priorities as _BRIEFING_PRIORITY, in the words a person would
# use, so the vector search pulls the material the popup actually needs rather
# than whatever happens to be newest.
_BRIEFING_QUERY = (
    "What should I remember before talking to {name} right now? Recent news in "
    "their life, promises either of us made, plans and trips, family and health "
    "updates, work or study changes, important dates, and what they are into."
)


def _by_briefing_priority(memories: list[dict]) -> list[dict]:
    """Most conversationally useful kinds of memory first.

    Stable sort, so within one type the caller's own ordering survives - which
    is recency on the Mongo path and relevance on the vector path.
    """
    return sorted(
        memories,
        key=lambda m: _BRIEFING_PRIORITY.index(m.get("type", "other"))
        if m.get("type", "other") in _BRIEFING_PRIORITY else len(_BRIEFING_PRIORITY),
    )


def _fallback_context(memories: list[dict]) -> list[str]:
    """Context bullets straight from the stored memory rows.

    Used when Gemini is unreachable or errors - the popup still shows real
    recall material instead of an empty card.
    """
    return [m["content"] for m in _by_briefing_priority(memories)[:6] if m.get("content")]


@router.post("/callers/lookup")
async def caller_lookup(
    payload: CallerLookupRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
):
    """Incoming phone number -> what to say to them.

    Single round trip for the call popup: match the number to a contact, pull
    their memories, and turn those into questions + context. Degrades in
    stages rather than failing - an unknown number, a known contact with no
    memories, and an unreachable Gemini each return a usable payload.
    """
    db = get_db()
    raw = (payload.phone_number or "").strip()
    key = contacts_mod.normalize_phone(raw)

    base = {
        "phone_number": raw,
        "found": False,
        "contact_id": None,
        "contact_name": None,
        "questions": [],
        "context": [],
        "memories_considered": 0,
        "degraded": None,
        # Overwritten once retrieval runs; present on every early return so the
        # client never has to branch on a missing key.
        "retrieval": None,
        "retrieval_error": None,
    }

    if not key:
        return {**base, "degraded": "unparseable_number"}

    # The phone number is the identifier. Everything below is found BY NUMBER
    # first; the contact row only supplies a display name. Memories written
    # before phone_key was stamped on them are still reachable through their
    # contact_id, so the two are OR'd together.
    contact_ids = [
        str(doc["_id"])
        async for doc in db.contacts.find({"phone_key": key}, {"_id": 1})
    ]
    memory_query: dict = (
        {"$or": [{"phone_key": key}, {"contact_id": {"$in": contact_ids}}]}
        if contact_ids
        else {"phone_key": key}
    )

    memories = []
    async for doc in db.memories.find(memory_query).sort("created_at", -1):
        doc.pop("_id", None)
        memories.append(doc)

    conversations = []
    recording_query = dict(memory_query)
    recording_query["status"] = "completed"
    async for doc in db.recordings.find(recording_query).sort("recorded_at", -1).limit(20):
        conversations.append(doc)

    contact = await db.contacts.find_one({"phone_key": key})
    if contact is None and not memories and not conversations:
        # Genuinely unknown caller - the popup shows the number and says so.
        return {**base, "degraded": "contact_not_found"}

    contact_id = str(contact["_id"]) if contact is not None else None
    name = (contact.get("name") if contact is not None else None) or raw
    base.update({"found": True, "contact_id": contact_id, "contact_name": name})

    if not memories and not conversations:
        # Known person, nothing remembered yet.
        return {**base, "degraded": "no_memories"}

    # Retrieval, first choice: the Actian VectorAI DB. There is no user
    # question here, so a standing briefing query stands in for one.
    rows, block, source, retrieval_error = await retrieve_context(
        _BRIEFING_QUERY.format(name=name),
        contact_id=contact_id,
        phone_key=key,
        api_key=x_gemini_api_key,
        limit=get_settings().vector_briefing_limit,
    )

    if source == "vector":
        # Relevance picked the set; priority decides what the model reads
        # first, exactly as on the Mongo path.
        ordered = _by_briefing_priority(
            [r for r in rows if r.get("kind") != vectordb.CONVERSATION]
        )
        block = _vector_block(
            [r for r in rows if r.get("kind") == vectordb.CONVERSATION] + ordered
        )
    else:
        ordered = _by_briefing_priority(memories)[:_BRIEFING_MEMORY_LIMIT]
        block = _memories_block(ordered, conversations)
        source = "mongodb"

    base["memories_considered"] = len(ordered)
    base["retrieval"] = source
    base["retrieval_error"] = retrieval_error

    try:
        result = await asyncio.to_thread(
            llm.caller_briefing, name, block, x_gemini_api_key
        )
    except Exception as exc:
        # The phone is ringing - never fail the popup over the LLM. Fall back
        # to raw memory rows as context, with no questions.
        log.warning("caller_briefing failed for %s: %s", name, exc)
        return {**base, "context": _fallback_context(ordered), "degraded": "llm_unavailable"}

    questions = [q for q in (result.get("questions") or []) if q and q.strip()]
    context = [c for c in (result.get("context") or []) if c and c.strip()]
    if not context:
        context = _fallback_context(ordered)

    if not questions and not context:
        # A known caller whose calls held nothing worth remembering. Say so
        # explicitly rather than leaving the popup to infer it from emptiness.
        return {**base, "degraded": "no_memories"}

    return {**base, "questions": questions[:5], "context": context[:6]}
