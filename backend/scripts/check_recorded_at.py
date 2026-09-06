"""Regression test for the "last conversation" timestamp bug.

The bug: last_conversation_at was set to *processing* time, not the actual
call time - so a backlog-processed recording from weeks ago showed up as
"just now". This proves three things against a RUNNING backend:

  1. recorded_at (sent by the app) actually drives the displayed timestamp.
  2. Processing an OLDER recording after a newer one does not regress
     last_conversation_at backwards (uses $max, not $set - a backlog scan
     rarely processes recordings in chronological order).
  3. Processing a genuinely NEWER recording still moves it forward.

Uses a throwaway contact (a fake phone number) so it never collides with
real demo data.

    .venv/Scripts/python.exe scripts/check_recorded_at.py [BASE_URL]
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
PHONE = "+910000000001"  # not a real contact - safe to reuse across runs
REAL_AUDIO = os.path.join("sample_audio", "Call_+919876543210_20260801_103000.wav")


def request(method, path, body=None, raw=None, content_type=None):
    url = BASE + path
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def multipart(fields, filename, content):
    boundary = "----memorybridge" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def die(message):
    print(f"\nFAILED: {message}")
    sys.exit(1)


def process_and_wait(label, days_ago, nonce):
    """Upload a (fake, distinct-hash) recording dated `days_ago` and wait for it
    to complete. Returns (contact_id, job)."""
    audio_bytes = open(REAL_AUDIO, "rb").read() if os.path.isfile(REAL_AUDIO) else b"x" * 100
    filename = f"regression_{nonce}.wav"
    digest = hashlib.sha256(f"{filename}:{nonce}".encode()).hexdigest()
    recorded_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    body, content_type = multipart(
        {
            "filename": filename,
            "hash": digest,
            "phone_number": PHONE,
            "contact_name": "Timestamp Regression Test",
            "recorded_at": recorded_at,
        },
        filename, audio_bytes,
    )
    status, started = request("POST", "/recordings/process", raw=body, content_type=content_type)
    if status != 200 or not started.get("job_id"):
        die(f"{label}: upload failed: {started}")
    job_id = started["job_id"]

    job = {}
    for _ in range(90):
        _, job = request("GET", f"/jobs/{job_id}")
        if job.get("status") in ("completed", "failed"):
            break
        time.sleep(2)
    if job.get("status") != "completed":
        die(f"{label}: job did not complete: {job.get('error')}")
    print(f"  {label}: uploaded with recorded_at={days_ago} days ago -> job completed")
    return job.get("contact_id"), job


def last_conversation_days_ago(contact_id):
    status, detail = request("GET", f"/contacts/{contact_id}/memories")
    if status != 200:
        die(f"could not load contact: {detail}")
    when = detail.get("last_conversation_at")
    if not when:
        die("contact has no last_conversation_at at all")
    parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - parsed).days, detail


print(f"Backend: {BASE}\n")
nonce = uuid.uuid4().hex[:8]

print("[1] Recording from 19 days ago")
contact_id, _ = process_and_wait("first", 19, f"{nonce}-a")
days, _ = last_conversation_days_ago(contact_id)
print(f"    last_conversation_at is {days} day(s) ago")
if not (17 <= days <= 21):
    die(f"expected ~19 days ago, got {days}")
print("    PASS - recorded_at (not processing time) drives the timestamp")

print("\n[2] Processing an OLDER recording (40 days ago) afterwards")
contact_id2, _ = process_and_wait("older, processed second", 40, f"{nonce}-b")
assert contact_id2 == contact_id, "same phone number should resolve to the same contact"
days, _ = last_conversation_days_ago(contact_id)
print(f"    last_conversation_at is {days} day(s) ago (must still be ~19, not ~40)")
if not (17 <= days <= 21):
    die(f"regression: out-of-order processing corrupted last_conversation_at "
        f"to {days} days ago - $max is not being used")
print("    PASS - backlog processing out of order did not move the date backwards")

print("\n[3] Processing a genuinely NEWER recording (2 days ago)")
contact_id3, _ = process_and_wait("newer, processed third", 2, f"{nonce}-c")
assert contact_id3 == contact_id
days, _ = last_conversation_days_ago(contact_id)
print(f"    last_conversation_at is {days} day(s) ago (must now be ~2)")
if not (0 <= days <= 4):
    die(f"expected ~2 days ago, got {days}")
print("    PASS - a genuinely newer conversation still moves the date forward")

print("\nALL RECORDED_AT REGRESSION CHECKS PASSED")
