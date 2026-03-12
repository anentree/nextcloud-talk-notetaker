# CLAUDE.md - Nextcloud Talk AI Notetaker

## Project Overview

Python service that monitors Nextcloud Talk for active calls, joins them via headless browser, records audio through in-browser WebRTC capture (MediaRecorder API), transcribes/summarizes with Gemini, uploads notes to Nextcloud via WebDAV, and emails participants.

## Architecture

```
Poll loop (10s)
  |
  +-- monitor.py    -- Talk API: detect new calls
  +-- recorder.py   -- Playwright + WebRTC: join call, record audio
  |   +-- Browser login (standard Nextcloud or Yunohost SSO)
  |   +-- In-browser audio capture via RTCPeerConnection interception
  |   +-- API-based call-end detection (inCall > 0 polling)
  |   +-- Extracts audio as base64 webm blob on call end
  +-- transcriber.py -- Chunked transcription pipeline:
  |   +-- ffprobe: detect audio duration
  |   +-- ffmpeg: split webm -> 15-min MP3 chunks
  |   +-- Gemini: transcribe each chunk, synthesize final notes
  +-- storage.py    -- WebDAV upload to Nextcloud (or local filesystem)
  +-- participants.py -- OCS API: resolve participant emails
  +-- mailer.py     -- SMTP: email notes to participants
```

## Critical Lessons Learned

### Authentication (recorder.py)

- **Yunohost SSO**: Must use browser form login, NOT API post. Cookies from `context.request.post()` do NOT propagate to `page.goto()`.
- **Standard Nextcloud**: Navigate to `/login`, fill `#user` and `#password`, submit the form.
- Selected by `AUTH_METHOD` env var (`nextcloud` or `yunohost`).

### Talk SPA Behavior (recorder.py)

- **Never use `networkidle`** for Talk pages. Talk maintains persistent WebSocket connections, so `networkidle` never resolves. Use `wait_for_load_state("load")` + `asyncio.sleep()`.
- **Multiple "Join call" buttons** exist in the DOM. Always use `.first` on locators.
- **First-run wizard overlays** may block interaction. Dismiss them in a loop before proceeding.

### Call-End Detection (recorder.py)

Poll the Talk API instead of checking UI elements:

```
GET /ocs/v2.php/apps/spreed/api/v4/room/{token}/participants
```

Check if any participant (other than the bot) has `inCall > 0`.

### Email Resolution (participants.py)

The bot user needs **subadmin** status (not admin) for user groups to read participant email addresses via the OCS API.

### In-Browser WebRTC Audio Capture (recorder.py)

1. `AUDIO_CAPTURE_INIT_JS` injected via `context.add_init_script()` BEFORE Talk loads
2. Patches RTCPeerConnection prototype methods + Proxy constructor
3. Remote audio captured through Web Audio API into MediaRecorder (webm/opus)
4. `getUserMedia` intercepted to return synthetic silent/black streams (prevents 440Hz beep from fake device)
5. Audio extracted as base64 on call end

Key Chrome flags: `--use-fake-ui-for-media-stream`, `--use-fake-device-for-media-stream`, `--autoplay-policy=no-user-gesture-required`, `--no-sandbox`

### Chunked Transcription Pipeline (transcriber.py)

- Short recordings (<=10 min): single-pass with MP3 conversion
- Long recordings: split into 15-min MP3 chunks, transcribe each, synthesize
- `ffmpeg -ss {start} -t 900 -ac 1 -ar 16000 -b:a 48k` per chunk (~5.3 MB each)
- `max_output_tokens=8192` per segment, `16384` for synthesis
- Sequential transcription with 2s pauses to avoid rate limits

### Bot Muting Strategy (recorder.py)

`--use-fake-device-for-media-stream` produces 440Hz beep + pacman animation. The working solution intercepts `getUserMedia` and returns synthetic streams (silent audio via `OscillatorNode` + zero `GainNode`, black video via `canvas.captureStream`). This is immune to Talk re-enabling tracks since the source itself is silent.

## Configuration

See `.env.example` for all configuration options and `README.md` for documentation.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pytest
```

## File Reference

| File              | Purpose                                                                       |
| ----------------- | ----------------------------------------------------------------------------- |
| `config.py`       | Loads `.env`, parses overrides, exposes `Config` dataclass                    |
| `main.py`         | Entry point, poll loop, orchestrates record->transcribe->save->email pipeline |
| `monitor.py`      | Polls Talk API for active calls, tracks which calls are new                   |
| `recorder.py`     | Playwright + WebRTC interception: join call, capture remote audio in-browser  |
| `transcriber.py`  | Chunked pipeline: split audio -> transcribe segments -> synthesize notes      |
| `storage.py`      | Uploads markdown to Nextcloud via WebDAV                                      |
| `participants.py` | Resolves participant emails (subadmin API -> override -> domain fallback)     |
| `mailer.py`       | Extracts follow-up email from notes, sends via SMTP                           |

## Hardening

- Gemini retry: 3 attempts with exponential backoff (5s, 10s, 20s) for 429 rate limiting
- Per-call error isolation: one failed call doesn't prevent processing the next
- Email failure tolerance: notes are saved even if email delivery fails
- Audio chunk flushing: browser audio flushed every 5 min to prevent OOM on long calls

## Known Limitations

- **Single-threaded**: concurrent calls are processed sequentially
- **Audio in /tmp**: wiped on reboot (fine since notes are saved to Nextcloud/local)
- **Transcription quirks**: Gemini may mishear proper nouns — inherent to speech-to-text
