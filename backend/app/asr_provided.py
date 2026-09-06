"""The provided ASR implementation, wired into the app/asr.py dispatch contract.

The actual engine lives in app/speech2text.py - vendored unmodified from the
implementation provided for this project (faster-whisper under the hood, CPU
or CUDA, no API key, runs fully offline). This file only adapts its Result
object to the plain `str` that app/asr.py's dispatcher expects.

ASR_PROVIDER=provided selects this. Config (all optional, via .env):
  STT_MODEL      tiny | base | small | medium | large-v3 | distil-large-v3
                 (default: base - good accuracy/speed balance on CPU)
  STT_DEVICE     cpu | cuda (default: auto-detected)
  STT_LANGUAGE   e.g. en, hi (default: auto-detected per file)
  STT_CACHE_DIR  where model weights are downloaded/cached

The model loads once, lazily, on first call, and is reused for every
subsequent recording in this process.
"""
from .speech2text import transcribe as _transcribe


def transcribe(audio_path: str) -> str:
    """audio file path -> transcript text. STTError (bad audio, missing
    dependency, decode failure) propagates up to app/pipeline.py, which marks
    the job failed with the message attached."""
    return _transcribe(audio_path).text
