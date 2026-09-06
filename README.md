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
    check_recorded_at.py    regression test for the "last conversation" timestamp fix
    cleanup_test_data.py    removes the throwaway contact check_recorded_at.py creates
mobile/           Flutter app (Android)
  assets/icon/      logo.png (legacy icon), logo_foreground.png (padded adaptive icon)
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

### App icon

The launcher icon is generated from `assets/icon/logo.png` / `logo_foreground.png` (a padded
copy for Android's adaptive-icon safe zone — see the comment in `pubspec.yaml`) via
[flutter_launcher_icons](https://pub.dev/packages/flutter_launcher_icons). To change the logo
later: replace those two source images, then

```bash
dart run flutter_launcher_icons
```

This regenerates every density (`mipmap-{m,h,x,xx,xxx}hdpi`) plus the adaptive-icon XML —
nothing else needs touching.

### Builds: where, which one, and why they're such different sizes

All builds land in `mobile/build/app/outputs/flutter-apk/`. Real sizes measured on this repo:

| Command | Output | Size | Use it for |
| --- | --- | --- | --- |
| `flutter build apk --debug` | `app-debug.apk` | ~155–175 MB | `flutter run` / local dev only |
| `flutter build apk --profile` | `app-profile.apk` | ~29 MB | performance profiling (DevTools) |
| `flutter build apk --release` | `app-release.apk` | ~52 MB | **sideloading — works on any device** |
| `flutter build apk --release --split-per-abi` | `app-arm64-v8a-release.apk` | ~19 MB | sideloading — 64-bit ARM (virtually every phone since ~2018) |
| ↳ same command | `app-armeabi-v7a-release.apk` | ~16 MB | older 32-bit ARM devices |
| ↳ same command | `app-x86_64-release.apk` | ~20 MB | emulators / x86 devices |
| `flutter build appbundle --release` | `app-release.aab` | smallest per install | **Play Store only** — not directly installable, Play delivers a ~15–20 MB slice per device |

**Debug vs release, concretely:** debug bundles an unstripped, JIT-capable Flutter engine (hot
reload needs it), keeps Dart assertions on, applies no shrinking/obfuscation, and — this is the
big one — packs native code for **all four** CPU architectures (arm64-v8a, armeabi-v7a, x86,
x86_64) into one APK so it runs on absolutely anything, debugger included. Release AOT-compiles
your Dart straight to machine code per architecture, strips debug info, shrinks/obfuscates via
R8, and tree-shakes unused assets (icon fonts here: 1.6 MB → 4 KB). None of that debug-only
weight belongs on a phone.

**Why is a 4-screen app tens of MB either way?** It mostly isn't your code. A size breakdown of
the arm64 release build (`flutter build apk --release --analyze-size --target-platform
android-arm64`) attributes 17.65 MB total:

| Piece | Size | What it is |
| --- | --- | --- |
| `libflutter.so` | 11.04 MB | **The Flutter engine itself** — Skia's renderer + the Dart runtime. Fixed cost; a blank Flutter app is nearly this size already. |
| `libapp.so` | 5.19 MB | Your actual compiled Dart — all four screens, every service, every plugin. |
| assets | 0.65 MB | The icon PNGs, tree-shaken fonts. |
| everything else | 0.77 MB | `classes.dex` (plugin Java/Kotlin glue), resources, manifest. |

So **91% of a release build is the engine, not the app** — this is the normal, expected shape
for any Flutter app, not something specific to MemoryBridge.

**The one lever that actually matters for a smaller file: `--split-per-abi`.** The default
`--release` build is "fat" — it bundles all three mobile architectures so it installs on any
device without knowing which. `--split-per-abi` builds one APK per architecture instead; hand
someone `app-arm64-v8a-release.apk` (~19 MB) rather than the ~52 MB universal one, since nearly
every phone sold since 2018 is arm64. That is the actual, safe way to get MemoryBridge down to a
much smaller file — there is no hidden bloat to trim out of a 4-screen app; the engine is simply
that size.

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

### "Last conversation" timestamps

The timestamp shown everywhere (dashboard contact list, contact screen, conversation cards) is
**when the call happened** (`recorded_at` — currently the recording file's own last-modified
time, sent by the app), never when the backend happened to process it. Those two differ whenever
a backlog of old recordings is scanned in one go — process a 3-week-old call today and it must
still read "3 weeks ago", not "just now".

Two things make that correct, in `app/pipeline.py` / `app/routers/contacts.py`:

- `recorded_at` is threaded through the whole pipeline and is what gets stored on the transcript,
  the recording, and the contact — `processed_at` / `created_at` remain separate, genuine
  processing-time audit fields, never shown as "last conversation".
- The contact's `last_conversation_at` is updated with Mongo's `$max`, not `$set`. A backlog scan
  rarely processes recordings in chronological order, so processing an *older* recording after a
  newer one must not drag the displayed date backwards. `scripts/check_recorded_at.py` is a
  regression test for exactly this (uploads out of chronological order, asserts the date only
  ever moves forward).

Also load-bearing: the Mongo client is `tz_aware=True` (`app/db.py`). Without it, every timestamp
the API returns loses its UTC marker in JSON, and Dart's `DateTime.parse` silently reinterprets
an offset-less string as *local* time — skewing every relative-time display by the phone's UTC
offset. This was a real, previously-undetected bug affecting every timestamp in the app, not only
`last_conversation_at`.

**Not yet done:** deriving `recorded_at` from a timestamp embedded in the filename itself (more
reliable than file mtime, which drifts if a recording is later copied/synced) — pending a real
sample filename to build the parser against, rather than guessing the date format.

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
