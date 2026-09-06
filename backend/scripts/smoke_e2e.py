"""End-to-end smoke test against a RUNNING backend.

Proves the milestone: audio -> transcript -> AI memories -> MongoDB -> answer.

    .venv/Scripts/python.exe scripts/smoke_e2e.py [BASE_URL] [GEMINI_KEY]

Needs MONGODB_URL configured on the server. GEMINI_KEY may instead come from
the GEMINI_API_KEY environment variable.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
GEMINI_KEY = (sys.argv[2] if len(sys.argv) > 2 else os.getenv("GEMINI_API_KEY", "")).strip()

FILENAME = "Call_+919876543210_20260801_103000.mp3"
SIDECAR = os.path.join("sample_audio", "Call_+919876543210_20260801_103000.txt")


def request(method, path, body=None, headers=None, raw=None, content_type=None):
    url = BASE + path
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def multipart(fields, file_field, filename, content):
    boundary = "----memorybridge" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def die(message):
    print(f"\nFAILED: {message}")
    sys.exit(1)


print(f"Backend: {BASE}\n")

# 1. health -------------------------------------------------------------------
print("[1] GET /health")
status, health = request("GET", "/health")
print("   ", json.dumps(health))
if status != 200:
    die("/health did not answer 200")
mongo = health.get("mongodb", {})
if not mongo.get("connected"):
    die(f"MongoDB not connected: {mongo.get('error')}")
if not mongo.get("write"):
    die("MongoDB connection has no write access")
print("    MongoDB read+write confirmed.")

# 2. build a recording --------------------------------------------------------
audio_bytes = open(SIDECAR, "rb").read() if os.path.isfile(SIDECAR) else b"fake audio bytes"
digest = hashlib.sha256(f"{FILENAME}:{len(audio_bytes)}:smoke".encode()).hexdigest()
print(f"\n[2] recording {FILENAME}  hash={digest[:16]}...")

# 3. duplicate check ----------------------------------------------------------
print("\n[3] POST /recordings/check")
status, check = request("POST", "/recordings/check",
                        {"recordings": [{"filename": FILENAME, "hash": digest}]})
print("   ", json.dumps(check))
if status != 200:
    die("/recordings/check failed")

# 4. process ------------------------------------------------------------------
print("\n[4] POST /recordings/process")
body, content_type = multipart(
    {"filename": FILENAME, "hash": digest, "phone_number": "+919876543210",
     "contact_name": "Rahul Sharma"},
    "file", FILENAME, audio_bytes,
)
headers = {"X-Gemini-Api-Key": GEMINI_KEY} if GEMINI_KEY else {}
status, started = request("POST", "/recordings/process", raw=body,
                          content_type=content_type, headers=headers)
print("   ", json.dumps(started))
job_id = started.get("job_id")
if not job_id:
    die("no job_id returned")

# 5. poll ---------------------------------------------------------------------
print(f"\n[5] GET /jobs/{job_id}")
job = {}
for _ in range(90):
    _, job = request("GET", f"/jobs/{job_id}")
    state = job.get("status")
    print(f"    status={state} stage={job.get('stage')}")
    if state in ("completed", "failed"):
        break
    time.sleep(2)
if job.get("status") != "completed":
    die(f"job did not complete: {job.get('error')}")
print(f"\n    summary: {job.get('summary')}")
print(f"    topics:  {job.get('topics')}")
print(f"    memories extracted: {job.get('memory_count')}")

# 6. duplicate protection -----------------------------------------------------
print("\n[6] re-check the same hash (must NOT be reprocessed)")
_, recheck = request("POST", "/recordings/check",
                     {"recordings": [{"filename": FILENAME, "hash": digest}]})
if recheck.get("new"):
    die("duplicate protection failed - recording offered for reprocessing")
print("    correctly reported as already processed.")

# 7. read memories back -------------------------------------------------------
contact_id = job.get("contact_id")
print(f"\n[7] GET /contacts/{contact_id}/memories")
status, detail = request("GET", f"/contacts/{contact_id}/memories")
if status != 200:
    die("could not load contact memories")
print(f"    {detail.get('name')} - {len(detail.get('memories', []))} memories")
for memory in detail.get("memories", []):
    print(f"      [{memory.get('type')}] {memory.get('content')}")

# 8. ask a question -----------------------------------------------------------
if GEMINI_KEY:
    question = "What should I ask Rahul when I call him next?"
    print(f"\n[8] POST /memory/search - {question!r}")
    status, answer = request("POST", "/memory/search",
                             {"contact_id": contact_id, "query": question},
                             headers={"X-Gemini-Api-Key": GEMINI_KEY})
    if status != 200:
        die(f"search failed: {answer}")
    print(f"\n    {answer.get('answer')}\n")
    for suggestion in answer.get("suggested_questions", []):
        print(f"      -> {suggestion}")
else:
    print("\n[8] skipped (no Gemini key supplied)")

print("\nEND-TO-END SMOKE TEST PASSED")
