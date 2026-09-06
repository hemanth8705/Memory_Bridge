"""Speech-to-text dispatch.

The contract every provider satisfies:

    audio file path (str)  ->  transcript (str)

Providers
---------
"provided" : YOUR working ASR implementation. Drop it into app/asr_provided.py
             (see that file for the one function it must expose). Selected with
             ASR_PROVIDER=provided.
"stub"     : Development stand-in so the rest of the pipeline is runnable and
             demoable before the real ASR is wired in. It does NOT do any
             speech recognition - it reads a sidecar transcript file.

Nothing here transcribes audio itself. This module only routes.
"""
import os
from typing import Callable

from .config import get_settings

DEMO_TRANSCRIPT = (
    "Hey! Sorry I missed your call earlier. Big news, I'm joining Microsoft next "
    "month, I signed the offer last week. I'm pretty nervous but excited about the "
    "team. Also we're going to Goa next weekend for a few days, finally taking a "
    "break. Oh and my mom's surgery is on Monday, so I might be a bit off next "
    "week. Anyway, remind me about that React course you mentioned - can you send "
    "me the link? Let's catch up properly after the trip."
)


class ASRUnavailable(RuntimeError):
    """Raised when the selected provider cannot run."""


def _stub_transcribe(audio_path: str) -> str:
    """Read a sidecar transcript instead of doing real ASR.

    Resolution order:
      1. <audio_path>.txt
      2. sample_audio/<basename-without-extension>.txt
      3. the uploaded file itself, if it is already a text file
      4. DEMO_TRANSCRIPT
    """
    candidates = [audio_path + ".txt"]
    base = os.path.splitext(os.path.basename(audio_path))[0]
    candidates.append(os.path.join("sample_audio", base + ".txt"))
    if audio_path.lower().endswith((".txt", ".md")):
        candidates.insert(0, audio_path)

    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if text:
                return text
    return DEMO_TRANSCRIPT


def _provided_transcribe(audio_path: str) -> str:
    try:
        from .asr_provided import transcribe as provided
    except ImportError as exc:
        raise ASRUnavailable(
            "ASR_PROVIDER=provided but app/asr_provided.py does not expose "
            "transcribe(audio_path: str) -> str."
        ) from exc
    return provided(audio_path)


_PROVIDERS: dict[str, Callable[[str], str]] = {
    "stub": _stub_transcribe,
    "provided": _provided_transcribe,
}


def active_provider() -> str:
    return get_settings().asr_provider


def transcribe(audio_path: str) -> str:
    """Transcribe an audio file. Blocking - callers run it in a thread."""
    name = active_provider()
    fn = _PROVIDERS.get(name)
    if fn is None:
        raise ASRUnavailable(
            f"Unknown ASR_PROVIDER={name!r}. Expected one of {sorted(_PROVIDERS)}."
        )
    if not os.path.isfile(audio_path):
        raise ASRUnavailable(f"Audio file not found: {audio_path}")
    text = (fn(audio_path) or "").strip()
    if not text:
        raise ASRUnavailable("ASR returned an empty transcript.")
    return text
