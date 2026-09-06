"""The processing pipeline.

  recording -> hash -> duplicate check -> identify contact -> ASR -> transcript
            -> save transcript -> Gemini -> structured JSON -> save memories

Runs as a FastAPI background task. The HTTP request that starts it returns a
job_id immediately; the client polls GET /jobs/{job_id}.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from pymongo import ReturnDocument

from . import asr, contacts as contacts_mod, llm, vectordb
from .db import get_db

log = logging.getLogger("memorybridge.pipeline")

# Job lifecycle
QUEUED = "queued"
TRANSCRIBING = "transcribing"
EXTRACTING = "extracting"
COMPLETED = "completed"
FAILED = "failed"
# Terminal, and NOT an error: the recording held no speech. Retrying would
# produce the same empty result forever, re-uploading and re-transcribing it
# on every scan, so these are never re-offered as new work.
NO_SPEECH = "no_speech"

# A recording that keeps failing for a real reason (backend down, bad audio)
# is retried, but not indefinitely.
MAX_ATTEMPTS = 3

# Statuses that must never be handed back to the app as "new work".
TERMINAL_STATUSES = (COMPLETED, NO_SPEECH)


async def create_job(hash_: str, filename: str) -> str:
    db = get_db()
    now = datetime.now(timezone.utc)
    result = await db.jobs.insert_one({
        "hash": hash_,
        "filename": filename,
        "status": QUEUED,
        "stage": "queued",
        "error": None,
        "created_at": now,
        "updated_at": now,
    })
    return str(result.inserted_id)


async def _set_status(job_id: str, status: str, **extra) -> None:
    """Update a job's status. Never raises.

    This is called from inside the pipeline's own error handler, so if it
    threw - a dropped Mongo connection, say - it would mask the original
    failure and leave the app polling a job that never resolves.
    """
    try:
        db = get_db()
        payload = {"status": status, "updated_at": datetime.now(timezone.utc), **extra}
        await db.jobs.update_one({"_id": ObjectId(job_id)}, {"$set": payload})
    except Exception:
        log.exception("Could not record job %s status=%s", job_id, status)


async def _set_recording(hash_: str, **fields) -> None:
    """Update a recording row. Never raises, for the same reason."""
    try:
        db = get_db()
        await db.recordings.update_one({"hash": hash_}, {"$set": fields})
    except Exception:
        log.exception("Could not update recording %s", hash_)


async def _fail(job_id: str, hash_: str, exc: BaseException, *,
                stage: str = "failed", note: str = "", **extra) -> None:
    """Record a genuine, retryable failure. Never raises.

    Counts attempts so a recording that can never succeed stops being retried
    instead of burning a transcription on every scan forever.
    """
    detail = f"{type(exc).__name__}: {exc}"
    attempts = 0
    try:
        db = get_db()
        result = await db.recordings.find_one_and_update(
            {"hash": hash_},
            {"$set": {"status": FAILED, "error": detail},
             "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        attempts = (result or {}).get("attempts", 0)
    except Exception:
        log.exception("Could not record failure for recording %s", hash_)

    await _set_status(
        job_id, FAILED, stage=stage, error=detail,
        attempts=attempts,
        give_up=attempts >= MAX_ATTEMPTS,
        note=note or None,
        **extra,
    )
    if attempts >= MAX_ATTEMPTS:
        log.error("Recording %s failed %d times; it will not be retried again.",
                  hash_, attempts)


async def get_job(job_id: str) -> Optional[dict]:
    db = get_db()
    try:
        oid = ObjectId(job_id)
    except Exception:
        return None
    return await db.jobs.find_one({"_id": oid})


def _rows(extraction: dict, field: str) -> list[dict]:
    """Read a list-of-objects field out of an LLM response, defensively.

    The schema asks for a list of objects, but a model can return a bare
    string, a list of strings, or null. None of that should take the whole
    pipeline down after transcription already succeeded, so anything that is
    not a usable object is skipped.
    """
    if not isinstance(extraction, dict):
        return []
    value = extraction.get(field)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(extraction: dict, field: str) -> list[str]:
    """Read a list-of-strings field out of an LLM response, defensively."""
    if not isinstance(extraction, dict):
        return []
    value = extraction.get(field)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _flatten_memories(extraction: dict) -> list[dict]:
    """Turn Gemini's structured output into individual memory rows.

    Tolerates a malformed response rather than raising: by the time this runs
    the recording has already been transcribed, and losing that work to a
    model that returned a slightly wrong shape would be the wrong trade.
    """
    out: list[dict] = []

    for fact in _rows(extraction, "facts"):
        if not fact.get("value"):
            continue
        out.append({
            "type": fact.get("type") or "other",
            "content": fact["value"],
            "confidence": fact.get("confidence", "medium"),
        })

    for person in _rows(extraction, "people"):
        bits = [person.get("name"), person.get("relationship")]
        who = " - ".join([b for b in bits if b]) or "someone"
        detail = person.get("event") or ""
        when = person.get("date") or ""
        content = f"{who}: {detail}".strip(": ").strip()
        if when:
            content = f"{content} ({when})"
        if detail:
            out.append({"type": "family", "content": content, "confidence": "medium"})

    for promise in _rows(extraction, "promises"):
        if not promise.get("description"):
            continue
        owed = promise.get("owed_by", "unclear")
        prefix = {"user": "You promised", "contact": "They promised"}.get(owed, "Promised")
        content = f"{prefix}: {promise['description']}"
        if promise.get("due"):
            content = f"{content} (by {promise['due']})"
        out.append({"type": "promise", "content": content, "confidence": "high",
                    "owed_by": owed})

    for item in _rows(extraction, "follow_ups"):
        if not item.get("description"):
            continue
        out.append({"type": "follow_up", "content": item["description"],
                    "confidence": "medium"})

    for date in _rows(extraction, "important_dates"):
        if not date.get("what"):
            continue
        out.append({
            "type": "important_date",
            "content": f"{date['what']} - {date.get('when', 'date unclear')}",
            "confidence": "high",
        })

    for interest in _strings(extraction, "interests"):
        out.append({"type": "interest", "content": f"Interested in {interest}",
                    "confidence": "medium"})

    return out


async def run_pipeline(job_id: str, audio_path: str, filename: str, hash_: str,
                       phone_number: Optional[str], contact_name: Optional[str],
                       gemini_api_key: Optional[str],
                       recorded_at: Optional[datetime] = None) -> None:
    """Full pipeline for one recording. Never raises - failures land on the job.

    recorded_at is when the CONVERSATION happened (the recording's own
    timestamp), not when we happen to process it - those can be weeks apart
    for a backlog scan. Everything conversation-dated uses recorded_at;
    "created_at" / "processed_at" fields stay as genuine processing-time
    audit stamps.
    """
    db = get_db()
    recorded_at = recorded_at or datetime.now(timezone.utc)
    try:
        # --- identify contact -------------------------------------------------
        identity = contacts_mod.identify(filename, phone_number, contact_name)
        contact = await contacts_mod.upsert_contact(db, identity)
        contact_id = str(contact["_id"])
        display_name = contact.get("name") or "Unknown caller"

        # The phone number is THE linking identifier. Stamp it onto every
        # document we write, not just the contact row, so a caller's memories
        # are findable by number alone - even if the contact row is missing,
        # renamed, or split in two.
        phone_key = identity.get("key")
        stored_number = identity.get("phone_number")
        await _set_status(job_id, TRANSCRIBING, stage="transcribing",
                          contact_id=contact_id, contact_name=display_name)

        # --- ASR --------------------------------------------------------------
        # A retry after a failed extraction must not pay for transcription
        # twice: if this recording was already transcribed, reuse it.
        transcript = None
        try:
            existing = await db.transcripts.find_one({"recording_hash": hash_})
            if existing and (existing.get("text") or "").strip():
                transcript = existing["text"]
                log.info("Reusing existing transcript for %s", filename)
        except Exception:
            log.exception("Could not check for an existing transcript; re-transcribing")

        if transcript is None:
            try:
                transcript = await asyncio.to_thread(asr.transcribe, audio_path)
            except asr.ASREmptyTranscript as exc:
                # Not a failure. The recording is real, it just has no speech -
                # silence, a missed call, a pocket dial. Marking it terminal
                # stops it being re-offered and re-transcribed on every scan.
                log.info("No speech in %s: %s", filename, exc)
                await _set_recording(
                    hash_,
                    status=NO_SPEECH,
                    contact_id=contact_id,
                    phone_key=phone_key,
                    phone_number=stored_number,
                    recorded_at=recorded_at,
                    memory_count=0,
                    summary="",
                    error=None,
                    processed_at=datetime.now(timezone.utc),
                )
                await _set_status(
                    job_id, COMPLETED, stage="no_speech",
                    contact_id=contact_id, contact_name=display_name,
                    memory_count=0, summary="",
                    note="No speech detected in this recording.",
                )
                return

        now = datetime.now(timezone.utc)
        transcript_doc = {
            "recording_hash": hash_,
            "contact_id": contact_id,
            "filename": filename,
            "text": transcript,
            "asr_provider": asr.active_provider(),
            "phone_key": phone_key,
            "phone_number": stored_number,
            "recorded_at": recorded_at,
            "created_at": now,
        }
        # Upsert on the recording hash: a retry reuses the transcript row
        # rather than stacking up duplicates of the same conversation.
        await db.transcripts.update_one(
            {"recording_hash": hash_}, {"$set": transcript_doc}, upsert=True,
        )
        saved = await db.transcripts.find_one({"recording_hash": hash_}, {"_id": 1})
        transcript_id = str(saved["_id"]) if saved else None

        # --- Gemini -----------------------------------------------------------
        await _set_status(job_id, EXTRACTING, stage="extracting_memories",
                          transcript_id=transcript_id)
        try:
            extraction = await asyncio.to_thread(
                llm.extract_memories, transcript, display_name, gemini_api_key
            )
        except Exception as exc:
            # The transcript is saved and safe. Fail the recording so it is
            # retried, but the retry now skips ASR entirely and only re-runs
            # the model - cheap enough to be worth attempting again.
            log.warning("Memory extraction failed for %s: %s", filename, exc)
            await _fail(
                job_id, hash_, exc,
                stage="extraction_failed",
                transcript_id=transcript_id,
                note="Transcribed successfully; memory extraction failed. "
                     "The transcript is saved, so a retry will not re-transcribe.",
            )
            return

        if not isinstance(extraction, dict):
            log.warning("Extraction for %s was %s, not an object; treating as empty",
                        filename, type(extraction).__name__)
            extraction = {}

        # --- save memories ----------------------------------------------------
        rows = _flatten_memories(extraction)
        now = datetime.now(timezone.utc)
        memory_docs = [
            {
                **row,
                "contact_id": contact_id,
                "phone_key": phone_key,
                "phone_number": stored_number,
                "source_recording": hash_,
                "source_transcript_id": transcript_id,
                "created_at": now,
            }
            for row in rows
        ]
        if memory_docs:
            result = await db.memories.insert_many(memory_docs)
            # Motor does not stamp _id back onto the dicts it was handed in
            # every version, so pair them up explicitly - vectordb keys its
            # point IDs off the Mongo _id to keep re-indexing idempotent.
            for doc, inserted_id in zip(memory_docs, result.inserted_ids):
                doc["_id"] = inserted_id

        # --- index into the vector DB ----------------------------------------
        # Second write layer: the same memories, embedded, so retrieval can be
        # semantic instead of "the most recent N rows". Mongo is still the
        # system of record, so a failure here is reported, not raised.
        try:
            vector_report = await vectordb.index_memories(
                memories=memory_docs,
                contact_id=contact_id,
                contact_name=display_name,
                phone_key=phone_key,
                phone_number=stored_number,
                recorded_at=recorded_at,
                summary=extraction.get("summary", ""),
                topics=extraction.get("topics", []),
                source_recording=hash_,
                source_transcript_id=transcript_id,
                api_key=gemini_api_key,
            )
        except Exception as exc:
            # Mongo is the system of record and already holds these memories.
            # An unreachable vector DB degrades retrieval quality; it must not
            # fail a recording that has otherwise been processed successfully.
            log.warning("Vector indexing failed for %s: %s", filename, exc)
            vector_report = {"indexed": 0, "error": f"{type(exc).__name__}: {exc}"}

        await db.recordings.update_one(
            {"hash": hash_},
            {"$set": {
                "status": COMPLETED,
                "contact_id": contact_id,
                "phone_key": phone_key,
                "phone_number": stored_number,
                "transcript_id": transcript_id,
                "summary": extraction.get("summary", ""),
                "topics": extraction.get("topics", []),
                "emotional_context": extraction.get("emotional_context", ""),
                "memory_count": len(rows),
                "vector_indexed": vector_report.get("indexed", 0),
                "recorded_at": recorded_at,
                "processed_at": now,
            }},
        )
        await db.contacts.update_one(
            {"_id": contact["_id"]},
            {
                # $max, not $set: a backlog scan can process recordings out of
                # chronological order, and "last conversation" must stay the
                # most recent CALL regardless of processing order.
                "$max": {"last_conversation_at": recorded_at},
                "$inc": {"conversation_count": 1},
            },
        )

        await _set_status(
            job_id, COMPLETED, stage="done",
            summary=extraction.get("summary", ""),
            topics=extraction.get("topics", []),
            memory_count=len(rows),
            vector_indexed=vector_report.get("indexed", 0),
            vector_error=vector_report.get("error"),
            transcript_preview=transcript[:500],
        )
        log.info("Processed %s for %s: %d memories, %d indexed into VectorAI DB%s",
                 filename, display_name, len(rows), vector_report.get("indexed", 0),
                 f" (vector error: {vector_report['error']})"
                 if vector_report.get("error") else "")

    except Exception as exc:
        # Catch-all for anything the staged handlers above did not cover:
        # contact upsert, Mongo writes, an unexpected engine error.
        log.exception("Pipeline failed for %s", filename)
        await _fail(job_id, hash_, exc)
    finally:
        # Uploaded audio is scratch space; the transcript is the artefact we keep.
        try:
            if audio_path and os.path.isfile(audio_path):
                os.remove(audio_path)
        except OSError:
            pass
