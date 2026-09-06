"""Pure-logic checks that need neither MongoDB nor a Gemini key.

    .venv/Scripts/python.exe scripts/test_logic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone  # noqa: E402

from app import asr, contacts, pipeline  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n        got:  {got!r}\n        want: {want!r}")
        failures.append(label)


print("\n[1] phone number extraction from filenames")
cases = [
    ("Call_+919876543210_20260801_103000.mp3", "+919876543210"),
    ("919876543210_20260801.mp3", "919876543210"),
    ("Call recording +91 98765 43210.m4a", "+919876543210"),
    ("+91-98765-43210_out.amr", "+919876543210"),
    ("Rahul_call_2026_08_01.mp3", None),
    ("20260801_103000.mp3", None),
    ("voice_memo.m4a", None),
    # The actual on-device recorder format: name(number)_YYYYMMDDHHMMSS
    ("Vaibhav Singh @ CI(08700648603)_20260906131241.mp3", "08700648603"),
    ("08920474604(08920474604)_20260906134028.mp3", "08920474604"),
]
for filename, want in cases:
    check(filename, contacts.extract_phone_number(filename), want)

print("\n[1b] call timestamp read from the filename, not the file's mtime")
check(
    "Vaibhav Singh @ CI(08700648603)_20260906131241.mp3",
    contacts.extract_recorded_at("Vaibhav Singh @ CI(08700648603)_20260906131241.mp3"),
    datetime(2026, 9, 6, 13, 12, 41, tzinfo=timezone.utc),
)
check(
    "08920474604(08920474604)_20260906134028.mp3",
    contacts.extract_recorded_at("08920474604(08920474604)_20260906134028.mp3"),
    datetime(2026, 9, 6, 13, 40, 28, tzinfo=timezone.utc),
)
check("no timestamp in filename -> None",
      contacts.extract_recorded_at("Rahul_call_2026_08_01.mp3"), None)
check("impossible date -> None (not a crash)",
      contacts.extract_recorded_at("X(919876543210)_20261399999999.mp3"), None)

print("\n[1c] a filename timestamp must never become a phone number")
# This actually happened: "08920474604(08920474604)_20260906134028.mp3" was
# parsed as the number 20260906134028, creating a contact named after the
# timestamp that held a real person's memories.
check("14-digit timestamp is recognised as one",
      contacts.looks_like_timestamp("20260906134028"), True)
check("normalize_phone refuses a timestamp",
      contacts.normalize_phone("20260906134028"), None)
check("a real 00-prefixed international number still survives",
      contacts.normalize_phone("00918700648603"), "8700648603")
check("impossible date is not a timestamp, so still a number",
      contacts.looks_like_timestamp("99999999999999"), False)
check("the exact filename that broke resolves to the right number",
      contacts.extract_phone_number("08920474604(08920474604)_20260906134028.mp3"),
      "08920474604")

print("\n[2] number normalisation (same person across formats)")
keys = {contacts.normalize_phone(n) for n in
        ("+919876543210", "919876543210", "09876543210", "9876543210", "+91 98765 43210")}
check("all five formats collapse to one key", len(keys), 1)
check("key is last 10 digits", keys.pop(), "9876543210")

print("\n[3] name fallback for name-based filenames")
check("Rahul_call_2026_08_01.mp3", contacts.guess_name("Rahul_call_2026_08_01.mp3"), "Rahul")
check("Call recording Sarah.m4a", contacts.guess_name("Call recording Sarah.m4a"), "Sarah")
check("name before the bracket is kept",
      contacts.guess_name("Vaibhav Singh @ CI(08700648603)_20260906131241.mp3"),
      "Vaibhav Singh @ CI")
check("number repeated as the name is NOT a name",
      contacts.guess_name("08920474604(08920474604)_20260906134028.mp3"), None)
check("digits-only filename leaves no name, not punctuation",
      contacts.guess_name("Call_+919876543210_20260801_103000.mp3"), None)

print("\n[4] identify() precedence - device contact beats filename")
ident = contacts.identify("Call_+919876543210_20260801.mp3", "+919876543210", "Rahul Sharma")
check("name from device", ident["name"], "Rahul Sharma")
check("key normalised", ident["key"], "9876543210")
unknown = contacts.identify("recording001.mp3")
check("unidentifiable falls back", unknown["name"], "Unknown caller")
check("unidentifiable flagged", unknown["identified"], False)

print("\n[5] ASR stub resolves the sidecar transcript")
audio = "sample_audio/Call_+919876543210_20260801_103000.mp3"
text = asr._stub_transcribe(audio)
check("sidecar transcript found", "Microsoft" in text and "Goa" in text, True)
check("did not fall through to canned demo text", text == asr.DEMO_TRANSCRIPT, False)

print("\n[6] Gemini output flattens into memory rows")
extraction = {
    "summary": "Rahul shared job and travel news.",
    "facts": [
        {"type": "job", "value": "Joining Microsoft next month", "confidence": "high"},
        {"type": "travel", "value": "Going to Goa next weekend", "confidence": "high"},
    ],
    "people": [{"relationship": "mother", "event": "knee surgery", "date": "Monday"}],
    "promises": [{"description": "Send the React course link", "owed_by": "user"}],
    "follow_ups": [{"description": "Ask how his mother's surgery went"}],
    "important_dates": [{"what": "Mother's surgery", "when": "Monday"}],
    "interests": ["React"],
    "topics": ["Microsoft", "Goa", "React"],
}
rows = pipeline._flatten_memories(extraction)
types_found = sorted({r["type"] for r in rows})
check("row count", len(rows), 7)
check("types", types_found,
      ["family", "follow_up", "important_date", "interest", "job", "promise", "travel"])
promise = next(r for r in rows if r["type"] == "promise")
check("promise attributed to the user", promise["content"],
      "You promised: Send the React course link")
mother = next(r for r in rows if r["type"] == "family")
check("family memory reads standalone", mother["content"], "mother: knee surgery (Monday)")
check("every row has content", all(r.get("content") for r in rows), True)

print("\n[7] empty extraction is handled, not crashed")
check("no memories from empty input", pipeline._flatten_memories({}), [])

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("All logic checks passed.")
