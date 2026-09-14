# Nextcloud Talk AI Notetaker

This bot will automatically summarize any Nextcloud Talk calls it's a participant of, using Google Gemini AI. Notes are saved to Nextcloud or locally, and optionally emailed to participants.

## How It Works

```
Poll loop (10s)
  |
  +-- monitor.py    -- Talk API: detect active calls
  +-- recorder.py   -- Playwright + WebRTC: join call, record audio
  +-- transcriber.py -- Gemini AI: transcribe + summarize (chunked pipeline)
  +-- storage.py    -- WebDAV: upload notes to Nextcloud
  +-- mailer.py     -- SMTP: email notes to participants
```

The bot joins calls as a regular participant, captures remote audio through in-browser WebRTC interception, and produces structured meeting notes with topics, action items, and decisions.

## Quick Start

```bash
git clone https://github.com/anentree/nextcloud-talk-notetaker.git
cd nextcloud-talk-notetaker
./setup.sh
```

The setup wizard will guide you through:

1. Checking Docker is installed
2. Connecting to your Nextcloud
3. Setting up a Gemini API key (free)
4. Choosing where to save notes
5. Optionally configuring email notifications

Re-running `./setup.sh` keeps a copy of your previous settings as `.env.bak.<timestamp>`.

## Prerequisites

- **Linux** with Docker (Ubuntu, Debian, Fedora, Arch, or any distro)
- **Nextcloud** with the **Talk** app installed
- **A dedicated bot user account** in Nextcloud
- **Gemini API key** (free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey))

Docker handles all internal dependencies (Python, ffmpeg, Chromium).

## Nextcloud User Setup

### 1. Create a bot user

In your Nextcloud admin panel, create a new user (e.g., `ai-notetaker`). This account does **not** need admin privileges.

### 2. Add bot to Talk rooms

Add the bot user as a participant in any Talk room you want it to monitor. The bot will automatically join active calls and start recording.

### 3. Set up notes folder (if using Nextcloud storage)

Create a folder in your Nextcloud (e.g., `meeting-notes`) and share it with the bot user. The bot saves notes into this shared folder.

### 4. Set up subadmin for email lookup (optional)

For the bot to look up participant email addresses, it needs **subadmin** status for user groups. This is **not** the same as admin — it only grants read access to user info within those groups.

In the Nextcloud admin panel:

1. Go to **Users** > **Groups**
2. For each group whose members should receive email notifications, add the bot user as a **subadmin** (not a regular member)

### 5. Notes filename

Notes are saved as `YYYY-MM-DD-HHMM-firstname-firstname.md`, with participant first names in alphabetical order. Set `FILENAME_LAST_USER` to always place a specific user's name last.

## Configuration

All configuration is via environment variables in `.env`:

| Variable                      | Required | Default                    | Description                                                                    |
| ----------------------------- | -------- | -------------------------- | ------------------------------------------------------------------------------ |
| `NEXTCLOUD_URL`               | Yes      | —                          | Nextcloud base URL (e.g., `https://cloud.example.com`)                         |
| `NEXTCLOUD_USER`              | Yes      | —                          | Bot username                                                                   |
| `NEXTCLOUD_PASSWORD`          | Yes      | —                          | Bot password (or app password)                                                 |
| `NEXTCLOUD_WEB_PASSWORD`      | No       | same as PASSWORD           | Separate browser login password (Yunohost SSO)                                 |
| `GEMINI_API_KEY`              | Yes      | —                          | Google Gemini API key                                                          |
| `AUTH_METHOD`                 | No       | `nextcloud`                | `nextcloud` (standard login) or `yunohost` (SSO)                               |
| `GEMINI_MODEL`                | No       | `gemini-2.5-flash-lite`    | Gemini model for transcription                                                 |
| `NOTES_STORAGE`               | No       | `nextcloud`                | `nextcloud` (WebDAV) or `local` (filesystem)                                   |
| `NOTES_FOLDER`                | No       | `/Talk/Notes`              | Nextcloud folder for notes (the wizard sets `/meeting-notes`)                  |
| `LOCAL_NOTES_DIR`             | No       | —                          | Local directory for notes (when storage=local)                                 |
| `FILENAME_LAST_USER`          | No       | —                          | User ID always placed last in filenames                                        |
| `POLL_INTERVAL_SECONDS`       | No       | `10`                       | Seconds between poll cycles                                                    |
| `AUDIO_DIR`                   | No       | `/tmp/notetaker-audio`     | Temp directory for audio files                                                 |
| `SMTP_HOST`                   | No       | `localhost`                | SMTP server                                                                    |
| `SMTP_PORT`                   | No       | `25`                       | SMTP port                                                                      |
| `SMTP_FROM`                   | No       | —                          | Sender email address                                                           |
| `SMTP_USER`                   | No       | —                          | SMTP username                                                                  |
| `SMTP_PASSWORD`               | No       | —                          | SMTP password                                                                  |
| `EMAIL_OVERRIDES`             | No       | —                          | Comma-separated `user=email` overrides                                         |
| `MAIL_DOMAIN`                 | No       | —                          | Fallback domain for email addresses                                            |
| `SMTP_TLS_VERIFY`             | No       | `auto`                     | `auto` verifies real hosts and skips only local hops; `true`/`false` force it  |
| `SMTP_TIMEOUT_SECONDS`        | No       | `30`                       | Connect/read timeout for SMTP                                                  |
| `STATE_DIR`                   | No       | `/data/notes`              | Persistent dir for the email spool and retained recordings (not a tmpfs)       |
| `HEARTBEAT_FILE`              | No       | `/tmp/notetaker-heartbeat` | Touched every poll; the container healthcheck reads its age                    |
| `RETAINED_AUDIO_MAX_AGE_DAYS` | No       | `30`                       | Days to keep a recording whose notes could not be produced                     |
| `NOTETAKER_VAD_THRESHOLD`     | No       | `0.02`                     | RMS level above which a track counts as speech for speaker attribution         |
| `SKIP_PREFLIGHT`              | No       | —                          | Set to `1` to start even if a startup dependency check fails (not recommended) |

## Notes Output

Each call produces a markdown file with:

- Meeting metadata (date, duration, participants)
- Topics discussed with detailed summaries
- Action items table with owners and deadlines
- Decisions made
- Open questions and follow-ups
- Follow-up email draft

## Architecture Notes

- **Audio capture**: In-browser WebRTC interception via Playwright. No PulseAudio needed.
- **Speaker attribution**: Per-track voice activity detection (VAD) with signaling-based identity. The bot intercepts HPB WebSocket messages to map each audio track to the participant who sent it — works reliably for calls of any size. Falls back to DOM scraping and Talk API if signaling is unavailable (e.g., non-HPB deployments).
- **Transcription**: Chunked pipeline — long recordings (>10 min) are split into 15-minute MP3 segments, each transcribed separately, then synthesized into final notes.
- **Muting**: The bot uses synthetic silent audio/black video streams so other participants don't hear Chrome's fake device beep.
- **Gemini cost**: ~$0.02 per 30-minute call on Flash Lite. Free tier (1000 req/day) is sufficient for most teams.
- **Failure recovery**: Every Gemini call retries transient errors (503 "high demand", 429, timeouts) for about six and a half minutes with backoff. If the notes still cannot be made, the recording is moved to `<state dir>/retained-audio/` together with a `.json` sidecar holding the recipients, speaker timeline and call time, and the poll loop retries it on its own — 15 minutes later, then doubling up to every 6 hours — until the notes are stored and emailed. Recordings are pruned after `RETAINED_AUDIO_MAX_AGE_DAYS` (30). A retained `.webm` with no sidecar predates this and needs `python -m notetaker.reprocess` by hand. Emails that cannot be sent are spooled and retried the same way.

## Troubleshooting

**Bot doesn't join calls unless it is added as a participant**: Make sure the bot user is added as a participant in the Talk room.

**"Could not connect" during setup**: Verify the Nextcloud URL is correct and starts with `https://`.

**"401 Unauthorized"**: Wrong bot username or password.

**"404 Not Found" on Talk API**: The Nextcloud Talk app may not be installed.

**No email notifications**: The bot needs subadmin status for user groups to look up email addresses. See setup instructions above.

**Container keeps restarting / setup says startup checks failed**: The notetaker checks Nextcloud, the notes folder, Gemini, SMTP and its state directory at startup and refuses to run if one fails. `docker compose logs notetaker | grep PREFLIGHT` names the one that failed; fix that value in `.env` and run `docker compose up -d`.

**Mail server on the same machine**: use `SMTP_HOST=host.docker.internal`, not `localhost` — inside the container `localhost` is the container.

**Transcription quality**: Gemini may mishear proper nouns. This is inherent to speech-to-text. For important terms, check the notes after generation.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pytest
```

## License

[AGPL-3.0](LICENSE) — same as Nextcloud.
