"""MongoDB access. Collections: recordings, transcripts, contacts, memories, jobs."""
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import get_settings

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


async def connect() -> None:
    """Open the connection and verify read/write access."""
    global _client, _db
    settings = get_settings()
    if not settings.mongodb_url:
        raise RuntimeError("MONGODB_URL is not set. Copy .env.example to .env and fill it in.")
    _client = AsyncIOMotorClient(settings.mongodb_url, serverSelectionTimeoutMS=8000)
    _db = _client[settings.mongodb_db]
    await _client.admin.command("ping")
    await _ensure_indexes(_db)


async def close() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client, _db = None, None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database is not connected.")
    return _db


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.recordings.create_index("hash", unique=True)
    # Not unique: contacts we could not identify all share phone_key=None, and a
    # unique index would reject every one after the first.
    await db.contacts.create_index("phone_key")
    await db.memories.create_index([("contact_id", 1), ("created_at", -1)])
    await db.transcripts.create_index("recording_hash")
    await db.jobs.create_index("created_at")


async def check_write_access() -> dict:
    """Verify the supplied connection string actually grants read+write."""
    db = get_db()
    result = {"read": False, "write": False, "error": None}
    try:
        await db.list_collection_names()
        result["read"] = True
        # Motor refuses attribute access for names starting with "_", so this
        # collection has to be addressed by key.
        healthcheck = db["_healthcheck"]
        probe = await healthcheck.insert_one({"probe": True})
        await healthcheck.delete_one({"_id": probe.inserted_id})
        result["write"] = True
    except Exception as exc:  # surfaced to /health so setup problems are obvious
        result["error"] = str(exc)
    return result
