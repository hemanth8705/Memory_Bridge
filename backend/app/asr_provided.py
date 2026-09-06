"""SLOT FOR THE PROVIDED ASR IMPLEMENTATION.

Paste the working speech-to-text code here (or import it here from wherever it
lives) and expose exactly one function:

    def transcribe(audio_path: str) -> str:
        ...returns the transcript text...

Then set ASR_PROVIDER=provided in .env. Nothing else in the codebase needs to
change - app/pipeline.py calls asr.transcribe() and does not care how the text
is produced.

Notes for whoever wires this up:
  * This runs inside a worker thread, so blocking model calls are fine.
  * Heavy models should be loaded once at module import, not per call.
  * Raise an exception on failure; the job will be marked "failed" with the
    message attached.
"""


def transcribe(audio_path: str) -> str:
    raise NotImplementedError(
        "The provided ASR implementation has not been pasted into "
        "app/asr_provided.py yet. Run with ASR_PROVIDER=stub until it is."
    )
