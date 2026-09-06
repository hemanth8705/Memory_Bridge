"""Failure-path checks: what happens when things go wrong.

Needs neither MongoDB nor a Gemini key - these exercise the classification and
defensive-parsing logic directly.

    .venv/Scripts/python.exe scripts/test_error_handling.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import asr, pipeline  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n        got:  {got!r}\n        want: {want!r}")
        failures.append(label)


def raises(label: str, fn, expected) -> None:
    try:
        fn()
    except expected:
        print(f"  PASS  {label}")
    except Exception as exc:
        print(f"  FAIL  {label}\n        raised {type(exc).__name__}: {exc}")
        failures.append(label)
    else:
        print(f"  FAIL  {label} - nothing raised")
        failures.append(label)


print("\n[1] a silent recording is an OUTCOME, not a failure")
# This is the real bug: VAD stripped all 14.6s of audio, ASR returned "", the
# recording was marked failed, and /recordings/check then re-offered it on
# every scan - re-uploading and re-transcribing it forever.
check("ASREmptyTranscript is a kind of ASRUnavailable",
      issubclass(asr.ASREmptyTranscript, asr.ASRUnavailable), True)
check("but it is a DISTINCT type, so it can be caught separately",
      asr.ASREmptyTranscript is asr.ASRUnavailable, False)

os.makedirs("uploads", exist_ok=True)
empty_path = os.path.join("uploads", "_test_empty.wav")
with open(empty_path, "wb"):
    pass
raises("a 0-byte file raises ASREmptyTranscript, not a generic error",
       lambda: asr.transcribe(empty_path), asr.ASREmptyTranscript)
os.remove(empty_path)

raises("a missing file is ASRUnavailable (a real error, worth retrying)",
       lambda: asr.transcribe("uploads/_does_not_exist.wav"), asr.ASRUnavailable)

print("\n[2] terminal statuses are never re-offered as new work")
check("completed is terminal", pipeline.COMPLETED in pipeline.TERMINAL_STATUSES, True)
check("no_speech is terminal", pipeline.NO_SPEECH in pipeline.TERMINAL_STATUSES, True)
check("failed is NOT terminal - real errors are retried",
      pipeline.FAILED in pipeline.TERMINAL_STATUSES, False)
check("retries are capped", pipeline.MAX_ATTEMPTS >= 1, True)

print("\n[3] malformed LLM output must not destroy a successful transcription")
# By the time extraction runs the audio is already transcribed. A model that
# returns a slightly wrong shape must not cost us that work.
for label, payload in [
    ("None instead of an object", None),
    ("a bare string", "sorry, I cannot help with that"),
    ("a list instead of an object", [1, 2, 3]),
    ("null fields", {"facts": None, "promises": None, "interests": None}),
    ("strings where objects were promised", {"facts": ["joined Microsoft"]}),
    ("numbers in a string list", {"interests": [1, 2, None, "React"]}),
    ("objects missing required keys", {"promises": [{}, {"due": "Monday"}]}),
    ("wrong type entirely for a field", {"follow_ups": "ask about his mum"}),
]:
    try:
        rows = pipeline._flatten_memories(payload)
        ok = isinstance(rows, list)
    except Exception as exc:
        ok = f"raised {type(exc).__name__}: {exc}"
    check(f"survives {label}", ok, True)

check("a usable item is still salvaged from a partly-broken list",
      pipeline._flatten_memories({"interests": [1, None, "React"]}),
      [{"type": "interest", "content": "Interested in React", "confidence": "medium"}])

print("\n[4] well-formed output is unaffected by the defensive parsing")
rows = pipeline._flatten_memories({
    "facts": [{"type": "job", "value": "Joining Microsoft", "confidence": "high"}],
    "promises": [{"description": "Send the React course", "owed_by": "user"}],
    "interests": ["Formula 1"],
})
check("row count", len(rows), 3)
check("promise attribution preserved",
      next(r for r in rows if r["type"] == "promise")["content"],
      "You promised: Send the React course")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("All error-handling checks passed.")
