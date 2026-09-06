"""
Drop-in speech-to-text. Offline, no API key, any audio format.

    from speech2text import transcribe
    text = transcribe("audio.mp3").text

Config via env vars (optional): STT_MODEL, STT_DEVICE, STT_LANGUAGE, STT_CACHE_DIR.
"""

from __future__ import annotations

import io
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Union

Audio = Union[str, Path, bytes, BinaryIO]

MODELS = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3")


class STTError(RuntimeError):
    """Bad audio, missing dependency, or decode failure."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def __str__(self) -> str:
        return f"[{self.start:7.2f} -> {self.end:7.2f}] {self.text}"


@dataclass
class Result:
    text: str
    language: str = ""
    confidence: float = 0.0
    duration: float = 0.0
    segments: list[Segment] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text)

    def dict(self) -> dict[str, Any]:
        return asdict(self)

    def json(self, **kw: Any) -> str:
        return json.dumps(self.dict(), **{"ensure_ascii": False, "indent": 2, **kw})

    def srt(self) -> str:
        def ts(sec: float) -> str:
            ms = int(sec * 1000)
            h, ms = divmod(ms, 3_600_000)
            m, ms = divmod(ms, 60_000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        return "\n".join(
            f"{i}\n{ts(s.start)} --> {ts(s.end)}\n{s.text}\n"
            for i, s in enumerate(self.segments, 1)
        )

    def save(self, path: str | Path) -> Path:
        """Write .txt, .srt, or .json - inferred from the suffix."""
        path = Path(path)
        writer = {".srt": self.srt, ".json": self.json}.get(
            path.suffix.lower(), lambda: self.text
        )
        path.write_text(writer(), encoding="utf-8")
        return path


class SpeechToText:
    """
    Thread-safe, lazily loaded transcriber. Instantiate once, reuse forever.
    Hardware is auto-detected unless you override it.
    """

    def __init__(
        self,
        model: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        cache_dir: str | None = None,
    ) -> None:
        self.model_name = model or os.getenv("STT_MODEL", "base")
        if self.model_name not in MODELS:
            raise ValueError(f"model must be one of {MODELS}, got {self.model_name!r}")

        self.device = device or os.getenv("STT_DEVICE") or self._best_device()
        self.compute_type = (
            compute_type or ("float16" if self.device == "cuda" else "int8")
        )
        self.language = language or os.getenv("STT_LANGUAGE") or None
        self.cache_dir = cache_dir or os.getenv("STT_CACHE_DIR") or None

        self._model: Any = None
        self._lock = threading.Lock()

    # ---------- public API ----------

    def transcribe(
        self,
        audio: Audio,
        *,
        language: str | None = None,
        vocabulary: str | None = None,  # bias toward names / jargon
        timestamps: bool = True,
        **options: Any,  # any faster-whisper kwarg passes through
    ) -> Result:
        """Transcribe fully and return a Result."""
        segments, info = self._run(audio, language, vocabulary, options)
        collected = [Segment(s.start, s.end, s.text.strip()) for s in segments]
        return Result(
            text=" ".join(s.text for s in collected).strip(),
            language=info.language,
            confidence=round(info.language_probability, 4),
            duration=round(info.duration, 2),
            segments=collected if timestamps else [],
        )

    def stream(
        self,
        audio: Audio,
        *,
        language: str | None = None,
        vocabulary: str | None = None,
        **options: Any,
    ) -> Iterator[Segment]:
        """Yield segments as they decode - for progress UI or long recordings."""
        segments, _ = self._run(audio, language, vocabulary, options)
        for s in segments:
            yield Segment(s.start, s.end, s.text.strip())

    __call__ = transcribe

    def warmup(self) -> "SpeechToText":
        """Force the model load now (e.g. at app startup) instead of on first call."""
        self._ensure_model()
        return self

    # ---------- internals ----------

    def _run(self, audio, language, vocabulary, options):
        model = self._ensure_model()
        options = {"beam_size": 5, "vad_filter": True, **options}
        try:
            return model.transcribe(
                self._resolve(audio),
                language=language or self.language,
                initial_prompt=vocabulary,
                **options,
            )
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"Transcription failed: {exc}") from exc

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:  # re-check inside lock
                    try:
                        from faster_whisper import WhisperModel
                    except ImportError as exc:
                        raise STTError(
                            "Missing dependency: pip install faster-whisper"
                        ) from exc
                    self._model = WhisperModel(
                        self.model_name,
                        device=self.device,
                        compute_type=self.compute_type,
                        download_root=self.cache_dir,
                    )
        return self._model

    @staticmethod
    def _resolve(audio: Audio):
        """Normalise path / bytes / stream into something the decoder accepts."""
        if isinstance(audio, bytes):
            if not audio:
                raise STTError("Audio bytes are empty")
            return io.BytesIO(audio)
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            if not path.is_file():
                raise STTError(f"Audio file not found: {path}")
            if path.stat().st_size == 0:
                raise STTError(f"Audio file is empty: {path}")
            return str(path)
        if hasattr(audio, "read"):
            return audio
        raise STTError(f"Unsupported audio input: {type(audio).__name__}")

    @staticmethod
    def _best_device() -> str:
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    def __repr__(self) -> str:
        return (
            f"SpeechToText(model={self.model_name!r}, device={self.device!r}, "
            f"compute={self.compute_type!r})"
        )


# ---------- module-level shortcut: shared instance, loaded once ----------

_default: SpeechToText | None = None
_default_lock = threading.Lock()


def engine(**kwargs: Any) -> SpeechToText:
    """The process-wide shared engine. kwargs apply only on first call."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = SpeechToText(**kwargs)
    return _default


def transcribe(audio: Audio, **kwargs: Any) -> Result:
    """One-liner entry point: transcribe("file.mp3").text"""
    return engine().transcribe(audio, **kwargs)


def stream(audio: Audio, **kwargs: Any) -> Iterator[Segment]:
    return engine().stream(audio, **kwargs)


# ---------- CLI: python speech2text.py audio.mp3 -o out.srt ----------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Transcribe an audio file to text.")
    p.add_argument("audio")
    p.add_argument("-m", "--model", default=None, help=f"one of {', '.join(MODELS)}")
    p.add_argument("-l", "--language", default=None, help="e.g. en, hi (default: auto)")
    p.add_argument("-o", "--out", default=None, help="output .txt / .srt / .json")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args()

    stt = SpeechToText(model=args.model, language=args.language)
    if not args.quiet:
        print(f"{stt}\nTranscribing {args.audio} ...\n")

    res = stt.transcribe(args.audio)

    if not args.quiet:
        for seg in res.segments:
            print(seg)
        print(f"\n[{res.language} {res.confidence:.0%} - {res.duration}s]\n")

    print(res.text)
    if args.out:
        print(f"\nSaved -> {res.save(args.out)}")