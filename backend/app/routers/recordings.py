"""Recording discovery, upload/processing, and job polling."""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import pipeline
from ..config import get_settings
from ..db import get_db

router = APIRouter()


def _parse_recorded_at(raw: Optional[str]) -> Optional[datetime]:
    """The app sends the recording's own timestamp (file mtime today; a
    filename-embedded timestamp will take priority once that format is
    nailed down) as an ISO 8601 string. Malformed input falls back to None
    rather than failing the whole upload."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RecordingRef(BaseModel):
    filename: str
    hash: str


class CheckRequest(BaseModel):
    recordings: list[RecordingRef] = Field(default_factory=list)


@router.post("/recordings/check")
async def check_recordings(payload: CheckRequest):
    """Given what the phone found on disk, return only what still needs processing."""
    db = get_db()
    hashes = [r.hash for r in payload.recordings if r.hash]
    if not hashes:
        return {"new": [], "already_processed": [], "in_progress": []}

    known = {}
    async for doc in db.recordings.find({"hash": {"$in": hashes}}, {"hash": 1, "status": 1}):
        known[doc["hash"]] = doc.get("status", "unknown")

    new, done, in_progress = [], [], []
    for ref in payload.recordings:
        status = known.get(ref.hash)
        item = {"filename": ref.filename, "hash": ref.hash}
        if status is None or status == pipeline.FAILED:
            new.append(item)          # never seen, or a previous attempt failed - retry
        elif status == pipeline.COMPLETED:
            done.append(item)
        else:
            in_progress.append(item)

    return {"new": new, "already_processed": done, "in_progress": in_progress}


@router.post("/recordings/process")
async def process_recording(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    filename: str = Form(...),
    hash: str = Form(...),
    phone_number: Optional[str] = Form(None),
    contact_name: Optional[str] = Form(None),
    recorded_at: Optional[str] = Form(None),
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
):
    """Accept one recording, queue it, and return a job_id straight away.

    The upload itself is the only thing the client waits on; transcription and
    memory extraction happen in the background.
    """
    db = get_db()

    existing = await db.recordings.find_one({"hash": hash})
    if existing and existing.get("status") == pipeline.COMPLETED:
        return {"job_id": existing.get("job_id"), "status": "already_processed",
                "hash": hash}
    if existing and existing.get("status") in (pipeline.QUEUED, pipeline.TRANSCRIBING,
                                               pipeline.EXTRACTING):
        return {"job_id": existing.get("job_id"), "status": "in_progress", "hash": hash}

    # The actual conversation date - NOT when we happen to process it. The
    # phone sends the file's own timestamp; if that's missing or malformed,
    # fall back to "now" rather than fail the upload over it.
    recorded_at_dt = _parse_recorded_at(recorded_at) or datetime.now(timezone.utc)

    settings = get_settings()
    suffix = os.path.splitext(file.filename or filename)[1] or ".audio"
    stored_path = os.path.join(settings.upload_dir, f"{uuid.uuid4().hex}{suffix}")
    size = 0
    with open(stored_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            out.write(chunk)
    if size == 0:
        os.remove(stored_path)
        raise HTTPException(status_code=400, detail="Uploaded file was empty.")

    job_id = await pipeline.create_job(hash, filename)
    now = datetime.now(timezone.utc)
    await db.recordings.update_one(
        {"hash": hash},
        {"$set": {
            "hash": hash,
            "filename": filename,
            "phone_number": phone_number,
            "contact_name": contact_name,
            "recorded_at": recorded_at_dt,
            "size_bytes": size,
            "status": pipeline.QUEUED,
            "job_id": job_id,
            "error": None,
            "uploaded_at": now,
        }},
        upsert=True,
    )

    background_tasks.add_task(
        pipeline.run_pipeline, job_id, stored_path, filename, hash,
        phone_number, contact_name, x_gemini_api_key, recorded_at_dt,
    )
    return {"job_id": job_id, "status": pipeline.QUEUED, "hash": hash}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = await pipeline.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    job.pop("_id", None)
    return {"job_id": job_id, **job}


@router.get("/recordings")
async def list_recordings(limit: int = 50):
    db = get_db()
    out = []
    async for doc in db.recordings.find().sort("uploaded_at", -1).limit(limit):
        doc.pop("_id", None)
        out.append(doc)
    return {"recordings": out}
