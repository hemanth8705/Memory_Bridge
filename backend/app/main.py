"""MemoryBridge backend."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import asr, db, vectordb
from .config import get_settings
from .routers import contacts as contacts_router, recordings as recordings_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memorybridge")

_startup_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_error
    try:
        await db.connect()
        log.info("MongoDB connected (db=%s)", get_settings().mongodb_db)
    except Exception as exc:
        # Keep the server up so /health can explain what is wrong.
        _startup_error = str(exc)
        log.error("MongoDB connection failed: %s", exc)
    # Opens the gRPC channel and creates the collection if it is missing.
    # Never raises: without it the app falls back to MongoDB-only retrieval.
    await vectordb.connect()
    yield
    await vectordb.close()
    await db.close()


app = FastAPI(title="MemoryBridge", version="0.1.0", lifespan=lifespan)

# The app talks to this over ngrok/LAN; this is a hackathon prototype.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(recordings_router.router, tags=["recordings"])
app.include_router(contacts_router.router, tags=["contacts"])


@app.get("/health")
async def health():
    """Setup check: is Mongo reachable, writable, and which ASR is active."""
    settings = get_settings()
    payload = {
        "status": "ok",
        "asr_provider": asr.active_provider(),
        "gemini_model": settings.gemini_model,
        "mongodb": {"connected": False, "database": settings.mongodb_db},
        "vector_db": await vectordb.status(),
    }
    if _startup_error:
        payload["status"] = "degraded"
        payload["mongodb"]["error"] = _startup_error
        return payload
    try:
        access = await db.check_write_access()
        payload["mongodb"].update({"connected": True, **access})
        if not access["write"]:
            payload["status"] = "degraded"
    except Exception as exc:
        payload["status"] = "degraded"
        payload["mongodb"]["error"] = str(exc)
    return payload


@app.get("/stats")
async def stats():
    """Numbers for the dashboard.

    Returns zeros with an error rather than a 500 when Mongo is unreachable -
    the dashboard should still render and say what is wrong.
    """
    try:
        database = db.get_db()
    except Exception as exc:
        return {"processed_recordings": 0, "contacts": 0, "memories": 0,
                "indexed_vectors": None, "error": str(exc)}
    try:
        return {
            "processed_recordings": await database.recordings.count_documents({"status": "completed"}),
            "contacts": await database.contacts.count_documents({}),
            "memories": await database.memories.count_documents({}),
            # Points held in the Actian VectorAI DB: one per memory, plus one
            # per conversation summary. None when the layer is unavailable.
            "indexed_vectors": await vectordb.count_for(),
        }
    except Exception as exc:
        log.warning("Could not read stats: %s", exc)
        return {"processed_recordings": 0, "contacts": 0, "memories": 0,
                "indexed_vectors": None, "error": str(exc)}
