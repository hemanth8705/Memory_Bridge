"""Verify the contract the native call popup depends on.

CallerPopupService.kt talks to /callers/lookup directly over HttpURLConnection
and parses the response with org.json. This replays exactly that request and
asserts every field the Kotlin parser reads is present with the type it
expects - so a backend change that would break the popup fails here instead of
during a real incoming call.

    .venv/Scripts/python.exe scripts/check_caller_popup.py [BASE_URL] [KNOWN_NUMBER]
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
KNOWN = sys.argv[2] if len(sys.argv) > 2 else "+919876543210"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def lookup(number: str) -> dict:
    """Same request the Kotlin service makes."""
    request = urllib.request.Request(
        f"{BASE}/callers/lookup",
        data=json.dumps({"phone_number": number}).encode(),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("ngrok-skip-browser-warning", "true")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def check_shape(label: str, payload: dict) -> None:
    """Every key CallerPopupService.parse() reads, with the expected type."""
    check(f"{label}: contact_name present", "contact_name" in payload)
    check(f"{label}: questions is a list",
          isinstance(payload.get("questions"), list),
          f"got {type(payload.get('questions')).__name__}")
    check(f"{label}: context is a list",
          isinstance(payload.get("context"), list),
          f"got {type(payload.get('context')).__name__}")
    check(f"{label}: every question is a string",
          all(isinstance(q, str) for q in payload.get("questions", [])))
    check(f"{label}: every context line is a string",
          all(isinstance(c, str) for c in payload.get("context", [])))
    check(f"{label}: 'degraded' key present (may be null)", "degraded" in payload)


print(f"Backend: {BASE}\n")

print(f"[1] known caller ({KNOWN})")
known = lookup(KNOWN)
check("found the contact", known.get("found") is True, str(known.get("degraded")))
check_shape("known", known)
check("returns 3-5 questions, per the MVP target",
      3 <= len(known.get("questions", [])) <= 5,
      f"got {len(known.get('questions', []))}")
check("returns at most 6 context lines",
      len(known.get("context", [])) <= 6,
      f"got {len(known.get('context', []))}")
check("questions are not generic filler", all(
    q.strip().lower().rstrip("?") not in {
        "how are you", "what's up", "whats up", "how is everything",
        "how was your trip", "how is work",
    }
    for q in known.get("questions", [])
))
print(f"\n    {known.get('contact_name')}")
print("    QUESTIONS")
for q in known.get("questions", []):
    print(f"      - {q}")
print("    CONTEXT")
for c in known.get("context", []):
    print(f"      - {c}")

print("\n[2] same person, local number format (must resolve identically)")
local = lookup("0" + "".join(ch for ch in KNOWN if ch.isdigit())[-10:])
check("matches the same contact",
      local.get("contact_id") == known.get("contact_id"),
      f"{local.get('contact_id')} != {known.get('contact_id')}")

print("\n[3] unknown number (popup must still render)")
unknown = lookup("+919999888877")
check("reports contact_not_found", unknown.get("degraded") == "contact_not_found")
check("found is False", unknown.get("found") is False)
check_shape("unknown", unknown)

print("\n[4] empty/withheld number (popup must not crash)")
blank = lookup("")
check("reports unparseable_number", blank.get("degraded") == "unparseable_number")
check_shape("blank", blank)

print("\n[5] Kotlin JSON quirk: optString() turns a JSON null into \"null\"")
# CallerPopupService guards for this; make sure the backend really does send
# null (not the string) so that guard stays the only thing needed.
raw = json.dumps(unknown)
check("contact_name is JSON null, not the string \"null\"",
      '"contact_name": null' in raw or '"contact_name":null' in raw,
      raw[:160])

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("CALL POPUP CONTRACT OK")
