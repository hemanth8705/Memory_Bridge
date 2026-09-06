"""Environment-driven configuration. No secrets are hardcoded."""
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.mongodb_url: str = os.getenv("MONGODB_URL", "").strip()
        self.mongodb_db: str = os.getenv("MONGODB_DB", "memorybridge").strip()
        # Optional server-side fallback. The Flutter app normally supplies the
        # user's own key per-request via the X-Gemini-Api-Key header.
        self.gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
        self.gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
        self.asr_provider: str = os.getenv("ASR_PROVIDER", "stub").strip().lower()
        self.upload_dir: str = os.getenv("UPLOAD_DIR", "./uploads").strip()
        os.makedirs(self.upload_dir, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
