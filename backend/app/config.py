"""Environment-driven configuration. No secrets are hardcoded."""
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

_FALSEY = {"0", "false", "no", "off", ""}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _int(name: str, default: int) -> int:
    """Env ints never fail startup - a typo falls back to the default."""
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.mongodb_url: str = os.getenv("MONGODB_URL", "").strip()
        self.mongodb_db: str = os.getenv("MONGODB_DB", "memorybridge").strip()
        # Optional server-side fallback. The Flutter app normally supplies the
        # user's own key per-request via the X-Gemini-Api-Key header.
        self.gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
        # gemini-2.0-flash was retired (confirmed via scripts/check_gemini_models.py -
        # the API now 404s on it). 2.5-flash is the cheapest currently-serving
        # model confirmed to handle our structured-output extraction; stay on
        # the flash tier (never pro) to keep per-call cost down.
        self.gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
        self.asr_provider: str = os.getenv("ASR_PROVIDER", "stub").strip().lower()
        self.upload_dir: str = os.getenv("UPLOAD_DIR", "./uploads").strip()
        os.makedirs(self.upload_dir, exist_ok=True)

        # --- Actian VectorAI DB (semantic memory layer) ---------------------
        # An index over Mongo, not a replacement for it. When it is off or
        # unreachable the app falls back to the Mongo path - see app/vectordb.py.
        self.vector_enabled: bool = _flag("VECTOR_DB_ENABLED", True)
        self.vector_url: str = os.getenv("VECTOR_DB_URL", "localhost:6574").strip()
        self.vector_api_key: str = os.getenv("VECTOR_DB_API_KEY", "").strip()
        self.vector_collection: str = os.getenv(
            "VECTOR_DB_COLLECTION", "memorybridge_memories").strip()
        self.vector_timeout: float = _float("VECTOR_DB_TIMEOUT", 15.0)
        # How many memories semantic search pulls back for a question, and for
        # the incoming-call briefing.
        self.vector_search_limit: int = _int("VECTOR_SEARCH_LIMIT", 24)
        self.vector_briefing_limit: int = _int("VECTOR_BRIEFING_LIMIT", 40)

        # gemini-embedding-001 is Matryoshka: 768 is a supported truncation and
        # keeps the index small. Changing either of these invalidates every
        # vector already stored - recreate the collection and re-index.
        self.embedding_model: str = os.getenv(
            "EMBEDDING_MODEL", "gemini-embedding-001").strip()
        self.embedding_dim: int = _int("EMBEDDING_DIM", 768)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
