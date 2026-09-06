"""Live check: a recording with no speech must not become a retry loop.

Reproduces the reported failure end to end against a RUNNING backend - a
silent recording used to be marked "failed", which made /recordings/check
re-offer it on every scan, re-uploading and re-transcribing it forever.

    .venv/Scripts/python.exe scripts/check_silent_recording.py [BASE_URL]
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
SILENT = os.path.join("sample_audio", "silence_test.wav")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def request(method, path, body=None, raw=None, content_type=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def multipart(fields, filename, content):
    boundary = "----mb" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
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


print(f"Backend: {BASE}\n")

if not os.path.isfile(SILENT):
    print(f"Missing {SILENT}; generate it first.")
    sys.exit(1)
audio = open(SILENT, "rb").read()

nonce = uuid.uuid4().hex[:8]
filename = f"Silent Caller(09999900001)_2026090612{nonce[:4]}.wav"
digest = hashlib.sha256(f"{filename}:{nonce}".encode()).hexdigest()

print(f"[1] uploading a 3s silent recording ({len(audio)} bytes)")
body, content_type = multipart(
    {"filename": filename, "hash": digest,
     "phone_number": "09999900001", "contact_name": "Silent Caller"},
    filename, audio,
)
status, started = request("POST", "/recordings/process", raw=body, content_type=content_type)
job_id = started.get("job_id")
check("upload accepted", status == 200 and bool(job_id), json.dumps(started))
if not job_id:
    sys.exit(1)

print("\n[2] waiting for the job")
job = {}
for _ in range(90):
    _, job = request("GET", f"/jobs/{job_id}")
    if job.get("status") in ("completed", "failed"):
        break
    time.sleep(2)
print(f"    status={job.get('status')} stage={job.get('stage')}")
print(f"    note={job.get('note')}")

check("job did NOT fail - no speech is an outcome, not an error",
      job.get("status") == "completed", f"status={job.get('status')} error={job.get('error')}")
check("stage says why", job.get("stage") == "no_speech", str(job.get("stage")))
check("no memories claimed", job.get("memory_count") in (0, None))

print("\n[3] THE REGRESSION: it must not be offered as new work again")
_, recheck = request("POST", "/recordings/check",
                     {"recordings": [{"filename": filename, "hash": digest}]})
offered = [r["hash"] for r in recheck.get("new", [])]
check("not in 'new' - it will not be re-uploaded or re-transcribed",
      digest not in offered,
      "STILL OFFERED: this is the infinite re-transcription loop")
check("reported as skipped, with a reason",
      any(r.get("hash") == digest for r in recheck.get("skipped", [])),
      json.dumps(recheck.get("skipped")))
skipped = next((r for r in recheck.get("skipped", []) if r.get("hash") == digest), {})
check("reason is no_speech", skipped.get("reason") == "no_speech", str(skipped))

print("\n[4] a second scan behaves identically (idempotent)")
_, again = request("POST", "/recordings/check",
                   {"recordings": [{"filename": filename, "hash": digest}]})
check("still not offered", digest not in [r["hash"] for r in again.get("new", [])])

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("SILENT-RECORDING HANDLING OK")
