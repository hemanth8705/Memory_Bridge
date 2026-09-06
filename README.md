# MemoryBridge

MemoryBridge is an AI relationship memory for ADHD. It captures and transcribes conversations,
extracts important people, promises, events and interests, and retrieves context when you need
it — helping you remember, reconnect, and have more meaningful conversations.

**MVP1 flow:** phone call recording → watched folder → caller identified from filename +
contacts → uploaded to the backend → speech-to-text → transcript in MongoDB → Gemini extracts
structured relationship memories → Flutter shows them, and answers questions about the person.

---

## Repository layout

```
backend/          FastAPI + MongoDB + Gemini
  app/
    main.py         app, /health, /stats
    config.py       env-driven settings (no secrets in code)
    db.py           Mongo connection + indexes + write-access check
    asr.py          speech-to-text DISPATCH ONLY
    asr_provided.py adapts speech2text.py's Result to the asr.py contract
    speech2text.py  the provided ASR implementation (faster-whisper), vendored as-is
    llm.py          Gemini prompt + structured-output schemas
    contacts.py     caller identification, contact upsert
    pipeline.py     hash -> dedupe -> identify -> ASR -> Gemini -> store
    routers/
      recordings.py /recordings/check, /recordings/process, /jobs/{id}
      contacts.py   /contacts, /contacts/{id}/memories, /memory/search
  scripts/
    test_logic.py           pure-logic checks (no Mongo, no API key needed)
    smoke_e2e.py            end-to-end proof against a running backend
    check_mongo.py          standalone MONGODB_URL connectivity + write check
    check_gemini_models.py  confirms a Gemini key + which flash models it can call
mobile/           Flutter app (Android)
  lib/
    services/config_service.dart      backend URL / Gemini key / folder
    services/api_service.dart         backend client + job polling
    services/recording_scanner.dart   folder scan, hashing, caller ID
    screens/                          setup, dashboard, contact, ask
```

---

## Backend

### 1. Configure

```bash
cd backend
cp .env.example .env      # then edit .env
```

`.env` keys:

| Key | What it is |
| --- | --- |
| `MONGODB_URL` | **You supply this.** Never commit it. |
| `MONGODB_DB` | Database name, defaults to `memorybridge`. |
| `GEMINI_API_KEY` | Optional, local testing only — the app sends the user's own key per request. |
| `GEMINI_MODEL` | Defaults to `gemini-2.5-flash`. Flash tier only, by design, to keep cost down — see below. |
| `ASR_PROVIDER` | `stub` or `provided` (see below). Defaults to `provided`. |
| `STT_MODEL` | `tiny` \| `base` \| `small` \| `medium` \| `large-v3` \| `distil-large-v3`. Defaults to `base`. |
| `UPLOAD_DIR` | Scratch space for uploaded audio. |

### Gemini model

`gemini-2.0-flash` (the earlier default) has been **retired** — the API now returns 404 for it.
`scripts/check_gemini_models.py` checks a key against the current flash-tier lineup (lists what
the key can see, then actually calls `generate_content` on each candidate, since listing can lag
behind what is really callable):

```bash
.venv/Scripts/python.exe scripts/check_gemini_models.py
```

`gemini-2.5-flash` is confirmed working and is the default. We deliberately stay on the flash
tier (never `pro`) everywhere to keep per-call cost down — that is a hard constraint, not just a
default, so don't switch `GEMINI_MODEL` to a `pro` variant without checking cost first.

### 2. Install and run

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Expose it to the phone

```bash
ngrok http 8000
```

Put the resulting `https://….ngrok-free.app` URL into the app's Setup screen.

### 4. Verify

```bash
curl http://127.0.0.1:8000/health
```

A good response has `"status": "ok"` and `mongodb.connected` **and** `mongodb.write` both
`true`. `/health` stays up even when Mongo is unreachable, and reports why — check it first
whenever something looks wrong.

Then:

```bash
.venv/Scripts/python.exe scripts/test_logic.py                 # no Mongo needed
.venv/Scripts/python.exe scripts/smoke_e2e.py http://127.0.0.1:8000 $GEMINI_KEY
```

`smoke_e2e.py` walks the whole milestone: health → dedupe check → upload → poll the job →
confirm the recording is not reprocessed → read memories back → ask a question.

---

## Speech-to-text

`app/asr.py` only **routes**; it never transcribes itself. Two providers:

- **`provided`** (default) — `app/speech2text.py`, vendored unmodified from the implementation
  given for this project. [faster-whisper](https://github.com/SYSTRAN/faster-whisper) under the
  hood: fully offline, no API key, CPU or CUDA auto-detected, decodes any common audio format
  via bundled PyAV (no system `ffmpeg` needed). The model loads once, lazily, on first call, and
  is reused for the rest of the process. `app/asr_provided.py` adapts its `Result` object to the
  plain `str` that `asr.py`'s dispatcher expects — that is the entire seam.
- **`stub`** — no speech recognition at all. Reads a sidecar transcript instead, so the rest of
  the pipeline is runnable without any audio. Resolution order: `<audio>.txt`, then
  `sample_audio/<name>.txt`, then the uploaded file if it is already text, then a built-in demo
  transcript. Useful for iterating on the Gemini prompt without waiting on transcription.

Verified end-to-end on this machine: a real spoken WAV (`sample_audio/`) → `app/speech2text.py`
→ an accurate transcript, in ~40s on CPU with the `base` model for ~40s of audio — see
`scripts/smoke_e2e.py`.

### Swapping in a different ASR implementation later

`app/asr_provided.py` only needs to keep exposing:

```python
def transcribe(audio_path: str) -> str: ...
```

Nothing else changes — `pipeline.py` calls `asr.transcribe()` and does not care how the text is
produced. It runs in a worker thread, so blocking model calls are fine; load heavy models once
at module import, not per call.

---

## Mobile app

```bash
cd mobile
flutter pub get
flutter run                  # or: flutter build apk --release
```

APKs land in `build/app/outputs/flutter-apk/`.

### Setup screen

Three user-supplied values, stored locally via `shared_preferences` — the app ships with no keys
of its own:

- **Backend URL** — the ngrok or LAN address.
- **Gemini API key** — the user's own, sent per request as `X-Gemini-Api-Key`.
- **Recording folder** — **Auto-detect** probes ~15 known recorder folders; **Browse** opens the
  system picker; or type a path like `/storage/emulated/0/Recordings/Call`.

**Test connection** verifies the backend and reports whether MongoDB is actually writable, so
setup problems surface before the demo rather than during it.

### Permissions

`MANAGE_EXTERNAL_STORAGE` (all-files access) so `dart:io` can list any recorder folder, plus
`READ_CONTACTS` to turn a caller's number into a name. Both are requested on save. All-files
access is a deliberate hackathon choice — it is reliable across recorder apps, and would not
pass Play Store review.

---

## How a recording becomes a memory

1. **Scan** — the app lists audio files in the configured folder.
2. **Identify** — the phone number is pulled from the filename and looked up in the device's
   contacts. Numbers are normalised to their last 10 digits, so `+919876543210`, `09876543210`
   and `9876543210` all resolve to one person. Name-based filenames like
   `Rahul_call_2026_08_01.mp3` fall back to a name guess.
3. **Deduplicate** — each file gets `SHA-256(filename + size + mtime)`. `POST /recordings/check`
   returns only what the backend has not already processed, so nothing is ever transcribed
   twice. Previously *failed* recordings are offered again.
4. **Process** — `POST /recordings/process` uploads the audio and returns a `job_id`
   immediately. Transcription never happens inside the request.
5. **Poll** — the app polls `GET /jobs/{job_id}` through
   `queued → transcribing → extracting → completed`.
6. **Store** — transcript, per-type memory rows, contact, and conversation summary all land in
   MongoDB.

Collections: `recordings`, `transcripts`, `contacts`, `memories`, `jobs`.

---

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Mongo connectivity + write access + active ASR provider |
| `GET /stats` | Dashboard counters |
| `POST /recordings/check` | `{recordings:[{filename,hash}]}` → what still needs processing |
| `POST /recordings/process` | Multipart upload → `{job_id}` |
| `GET /jobs/{job_id}` | Job status, and results once completed |
| `GET /contacts` | People, most recently spoken to first |
| `GET /contacts/{id}/memories` | Memories grouped by type, topics, conversations |
| `POST /memory/search` | `{contact_id, query}` → grounded answer + suggested questions |

The Gemini key travels on the `X-Gemini-Api-Key` header for the two endpoints that need it.

---

## Demo script

1. Show a recording in the folder, e.g. `Call_+919876543210_20260801_103000.mp3`.
2. **Scan recordings** → it appears as new, identified as the contact.
3. **Process new recordings** → `Transcribing… → Extracting memories…`
4. Open the contact:
   - ✓ Joining Microsoft
   - ✓ Going to Goa
   - ✓ Mother's surgery Monday
   - ✓ React course promised
5. **Ask memory** → *"What should I ask Rahul when I call him next?"*
6. Press **Scan** again — the recording is **not** offered for reprocessing.

Step 6 is worth showing: it is the difference between a demo and a system.

---

## Known limits (MVP1)

- Background jobs live in the FastAPI process. Restarting the server abandons in-flight jobs;
  the recording is retried on the next scan because failed rows are re-offered.
- Periodic hourly scanning is not implemented — manual **Scan & Process** is the supported path.
- No authentication. Anyone with the ngrok URL can reach the API.
- `/memory/search` sends a contact's memories to Gemini rather than doing vector retrieval.
  Fine at MVP1 volumes, and the natural place for RAG later.
- The `base` Whisper model transcribes roughly 1:1 with audio duration on a CPU (~40s for a
  ~40s call in testing here). A real device or a smaller phone-side CPU will be slower — that is
  why processing is a polled background job rather than a held-open request. If a demo call runs
  long, `STT_MODEL=tiny` trades some accuracy for speed.
