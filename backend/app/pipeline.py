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

from . import asr, contacts as contacts_mod, llm
from .db import get_db

log = logging.getLogger("memorybridge.pipeline")

# Job lifecycle
QUEUED = "queued"
TRANSCRIBING = "transcribing"
EXTRACTING = "extracting"
COMPLETED = "completed"
FAILED = "failed"


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
    db = get_db()
    payload = {"status": status, "updated_at": datetime.now(timezone.utc), **extra}
    await db.jobs.update_one({"_id": ObjectId(job_id)}, {"$set": payload})


async def get_job(job_id: str) -> Optional[dict]:
    db = get_db()
    try:
        oid = ObjectId(job_id)
    except Exception:
        return None
    return await db.jobs.find_one({"_id": oid})


def _flatten_memories(extraction: dict) -> list[dict]:
    """Turn Gemini's structured output into individual memory rows."""
    out: list[dict] = []

    for fact in extraction.get("facts") or []:
        if not fact.get("value"):
            continue
        out.append({
            "type": fact.get("type") or "other",
            "content": fact["value"],
            "confidence": fact.get("confidence", "medium"),
        })

    for person in extraction.get("people") or []:
        bits = [person.get("name"), person.get("relationship")]
        who = " - ".join([b for b in bits if b]) or "someone"
        detail = person.get("event") or ""
        when = person.get("date") or ""
        content = f"{who}: {detail}".strip(": ").strip()
        if when:
            content = f"{content} ({when})"
        if detail:
            out.append({"type": "family", "content": content, "confidence": "medium"})

    for promise in extraction.get("promises") or []:
        if not promise.get("description"):
            continue
        owed = promise.get("owed_by", "unclear")
        prefix = {"user": "You promised", "contact": "They promised"}.get(owed, "Promised")
        content = f"{prefix}: {promise['description']}"
        if promise.get("due"):
            content = f"{content} (by {promise['due']})"
        out.append({"type": "promise", "content": content, "confidence": "high",
                    "owed_by": owed})

    for item in extraction.get("follow_ups") or []:
        if not item.get("description"):
            continue
        out.append({"type": "follow_up", "content": item["description"],
                    "confidence": "medium"})

    for date in extraction.get("important_dates") or []:
        if not date.get("what"):
            continue
        out.append({
            "type": "important_date",
            "content": f"{date['what']} - {date.get('when', 'date unclear')}",
            "confidence": "high",
        })

    for interest in extraction.get("interests") or []:
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
        transcript = await asyncio.to_thread(asr.transcribe, audio_path)

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
        transcript_result = await db.transcripts.insert_one(transcript_doc)
        transcript_id = str(transcript_result.inserted_id)

        # --- Gemini -----------------------------------------------------------
        await _set_status(job_id, EXTRACTING, stage="extracting_memories",
                          transcript_id=transcript_id)
        extraction = await asyncio.to_thread(
            llm.extract_memories, transcript, display_name, gemini_api_key
        )

        # --- save memories ----------------------------------------------------
        rows = _flatten_memories(extraction)
        now = datetime.now(timezone.utc)
        if rows:
            await db.memories.insert_many([
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
            ])

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
            transcript_preview=transcript[:500],
        )
        log.info("Processed %s for %s: %d memories", filename, display_name, len(rows))

    except Exception as exc:
        log.exception("Pipeline failed for %s", filename)
        await _set_status(job_id, FAILED, stage="failed", error=f"{type(exc).__name__}: {exc}")
        await db.recordings.update_one(
            {"hash": hash_},
            {"$set": {"status": FAILED, "error": str(exc)}},
        )
    finally:
        # Uploaded audio is scratch space; the transcript is the artefact we keep.
        try:
            if audio_path and os.path.isfile(audio_path):
                os.remove(audio_path)
        except OSError:
            pass
