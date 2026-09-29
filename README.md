# Nextcloud Talk AI Notetaker

This bot will automatically summarize any Nextcloud Talk calls it's a participant of. Transcription runs locally with Whisper or in the cloud with Google Gemini; Gemini writes the notes. Notes are saved to Nextcloud or locally, and optionally emailed to participants.

## How It Works

```
Poll loop (10s)
  |
  +-- monitor.py    -- Talk API: detect active calls
  +-- recorder.py   -- Playwright + WebRTC: join call, record audio
  +-- whisper.py    -- faster-whisper (optional): transcribe on this machine
  +-- attribution.py -- speaker timeline -> "[MM:SS] Name: words"
  +-- transcriber.py -- Gemini: write notes (or transcribe + write when the backend is gemini)
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
3. Choosing where transcription runs (Whisper on this machine, or Gemini)
4. Setting up a Gemini API key (free)
5. Choosing where to save notes
6. Optionally configuring email notifications

Re-running `./setup.sh` keeps a copy of your previous settings as `.env.bak.<timestamp>`.

## Prerequisites

- **Linux** with Docker (Ubuntu, Debian, Fedora, Arch, or any distro)
- **Nextcloud** with the **Talk** app installed
- **A dedicated bot user account** in Nextcloud
- **Gemini API key** (free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- **For Whisper transcription (optional):** an x86_64 CPU with AVX2 and 4+ cores, 8 GB RAM, 5 GB disk. ARM boards and CPUs without AVX2 are too slow; use Gemini there.

Docker handles all internal dependencies (Python, ffmpeg, Chromium).

## Nextcloud User Setup

### 1. Create a bot user

In your Nextcloud admin panel, create a new user (e.g., `ai-notetaker`). This account does **not** need admin privileges.

### 2. Add bot to Talk rooms

Add the bot user as a participant in any Talk room you want it to monitor. The bot will automatically join active calls and start recording.

### 3. Set up notes folder (if using Nextcloud storage)

Create a folder in your Nextcloud (e.g., `meeting-notes`) and share it with the bot user. The bot saves notes into this shared folder.

To also keep the word-for-word transcript of each call (Whisper backend only), create a second folder (e.g., `meeting-notes-transcriptions`), share it with the bot user **only**, and set `TRANSCRIPTS_FOLDER` to it. Transcripts are never emailed; anyone the notes folder is shared with sees the summaries, not the transcripts. The bot refuses to start if `TRANSCRIPTS_FOLDER` is missing, or is the notes folder, inside or above it, or shares a parent folder with it — the check sees folder names, not shares, so it refuses every layout where one share could cover both. It checks again before every save, so `SKIP_PREFLIGHT` does not bypass it.

### 4. Set up subadmin for email lookup (optional)

For the bot to look up participant email addresses, it needs **subadmin** status for user groups. This is **not** the same as admin — it only grants read access to user info within those groups.

In the Nextcloud admin panel:

1. Go to **Users** > **Groups**
2. For each group whose members should receive email notifications, add the bot user as a **subadmin** (not a regular member)

### 5. Notes filename

Notes are saved as `YYYY-MM-DD-HHMM-firstname-firstname.md`, with participant first names in alphabetical order. Set `FILENAME_LAST_USER` to always place a specific user's name last.

## Transcription

After a call, the recording is turned into text, then Gemini writes the notes from that text. `TRANSCRIPTION_BACKEND` chooses who does the first step:

- **`whisper`** — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) runs on this machine (`large-v3-turbo`, CPU, int8). Free, no daily limit, and the audio never leaves your server. Gemini then gets one small text request per call, so the free Gemini plan covers ~20 calls a day. Speaker names come from the call's own audio tracks, assigned in code — Gemini cannot invent a speaker. Needs the one-time 1.6 GB model download and a decent CPU: on a 12-thread desktop CPU (Ryzen 5 2600) a one-hour call takes about 45 minutes; on a 4-thread CPU about 60 minutes. The bot cannot join another call while it is transcribing.
- **`gemini`** — the audio is sent to Google in 15-minute chunks (six requests per hour-long call). Works on any hardware, but the free plan covers about 3 one-hour calls a day and requests are delayed when Google reports "high demand". Turning on billing for the key removes the cap (about $0.02 per one-hour call).

**Switching to Whisper later** — in this order, because the container refuses to start with the backend set and no model on disk:

```bash
docker compose run --rm --no-deps notetaker download-model   # 1.6 GB, resumable
# then in .env: TRANSCRIPTION_BACKEND=whisper and NOTETAKER_MEMORY_LIMIT=6g
docker compose up -d                                           # `restart` does NOT apply .env changes
```

**Switching back** — set `TRANSCRIPTION_BACKEND=gemini` (and `NOTETAKER_MEMORY_LIMIT=2g`), `docker compose up -d`. The model stays in `./models`; `sudo rm -rf ./models` removes it (the files are root-owned, like `notes/`).

If Whisper fails on a recording it is retried like any other failure; after two failures that recording is transcribed by Gemini instead. GPU transcription is not supported (it would need a CUDA base image).

## Configuration

All configuration is via environment variables in `.env`:

| Variable                      | Required | Default                    | Description                                                                    |
| ----------------------------- | -------- | -------------------------- | ------------------------------------------------------------------------------ |
| `NEXTCLOUD_URL`               | Yes      | —                          | Nextcloud base URL (e.g., `https://cloud.example.com`)                         |
| `NEXTCLOUD_USER`              | Yes      | —                          | Bot username                                                                   |
| `NEXTCLOUD_PASSWORD`          | Yes      | —                          | Bot password (or app password)                                                 |
| `NEXTCLOUD_WEB_PASSWORD`      | No       | same as PASSWORD           | Separate browser login password (Yunohost SSO)                                 |
| `GEMINI_API_KEY`              | Unless `NOTES_BACKEND=claude` | —            | Google Gemini API key                                                          |
| `AUTH_METHOD`                 | No       | `nextcloud`                | `nextcloud` (standard login) or `yunohost` (SSO)                               |
| `GEMINI_MODEL`                | No       | `gemini-2.5-flash-lite`    | Gemini model for the notes (and transcription when the backend is gemini)      |
| `TRANSCRIPTION_BACKEND`       | No       | `gemini`                   | `whisper` (local, private, needs model download) or `gemini` (Google hears)    |
| `NOTES_BACKEND`               | No       | `gemini`                   | Who writes the notes from a Whisper transcript: `gemini`, or `claude` (subscription, nothing to Google) |
| `CLAUDE_MODEL`                | No       | `haiku`                    | Claude model when `NOTES_BACKEND=claude`                                        |
| `CLAUDE_CODE_OAUTH_TOKEN`     | With `NOTES_BACKEND=claude` | —         | Subscription token from `claude setup-token`                                    |
| `WHISPER_MODEL`               | No       | `large-v3-turbo`           | faster-whisper model name; `small` is about twice as fast and less accurate    |
| `WHISPER_LANGUAGE`            | No       | `en`                       | Two-letter language code for Whisper; empty = auto-detect                      |
| `WHISPER_MODEL_DIR`           | No       | `/data/models`             | Model files inside the container (`./models` on the host)                      |
| `NOTETAKER_MEMORY_LIMIT`      | No       | `2g`                       | Container memory cap read by docker-compose.yml; wizard sets `6g` for Whisper  |
| `NOTES_STORAGE`               | No       | `nextcloud`                | `nextcloud` (WebDAV) or `local` (filesystem)                                   |
| `NOTES_FOLDER`                | No       | `/Talk/Notes`              | Nextcloud folder for notes (the wizard sets `/meeting-notes`)                  |
| `LOCAL_NOTES_DIR`             | No       | —                          | Local directory for notes (when storage=local)                                 |
| `TRANSCRIPTS_FOLDER`          | No       | —                          | Nextcloud folder for the verbatim Whisper transcript; never emailed; top-level, separate from `NOTES_FOLDER` |
| `LOCAL_TRANSCRIPTS_DIR`       | No       | —                          | Local directory for transcripts (when storage=local)                           |
| `FILENAME_LAST_USER`          | No       | —                          | User ID always placed last in filenames                                        |
| `POLL_INTERVAL_SECONDS`       | No       | `10`                       | Seconds between poll cycles                                                    |
| `AUDIO_DIR`                   | No       | `/tmp/notetaker-audio`     | Temp directory for audio files                                                 |
| `SMTP_HOST`                   | No       | —                          | SMTP server. Leave it unset to disable email entirely (notes are still stored) |
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

With `TRANSCRIPTS_FOLDER` set, a second file with the same name goes into that folder: the verbatim Whisper transcript, one `[MM:SS] Speaker: words` line per utterance. It is only written, never emailed. Calls whose notes came from the Gemini audio path (Gemini backend, or the fallback after repeated Whisper failures) have no verbatim transcript.

## Architecture Notes

- **Audio capture**: In-browser WebRTC interception via Playwright. No PulseAudio needed.
- **Speaker attribution**: Per-track voice activity detection (VAD) with signaling-based identity. The bot intercepts HPB WebSocket messages to map each audio track to the participant who sent it — works reliably for calls of any size. Falls back to DOM scraping and Talk API if signaling is unavailable (e.g., non-HPB deployments).
- **Transcription**: with the `whisper` backend the whole recording is transcribed locally in one pass and speakers are assigned from the per-track timeline; with `gemini`, long recordings (>10 min) are split into 15-minute MP3 segments, each transcribed by Gemini, then synthesized into notes.
- **Muting**: The bot uses synthetic silent audio/black video streams so other participants don't hear Chrome's fake device beep.
- **Gemini cost**: the free plan allows 20 requests a day per model — about 3 one-hour calls with the `gemini` backend, about 20 with `whisper`. With billing on, a one-hour call costs about $0.02 on Flash Lite.
- **Failure recovery**: Every Gemini call retries transient errors (503 "high demand", 429, timeouts) for about six and a half minutes with backoff. If the notes still cannot be made, the recording is moved to `<state dir>/retained-audio/` together with a `.json` sidecar holding the recipients, speaker timeline and call time, and the poll loop retries it on its own — 15 minutes later, then doubling up to every 6 hours — until the notes are stored (an email that could not be sent waits in the spool and is retried separately). A retry reuses the transcript and skips the email if the first attempt got that far, so nobody is emailed twice. With the Whisper backend the local transcript is kept the same way, so a Gemini outage after a 45-minute transcription costs one text request on retry, not another 45 minutes. If Gemini is still unavailable when a retry runs, the whole recording is kept for the next one; only a segment that fails for a reason that will not change (an empty answer on the same input) is skipped, and the notes then say which minutes are missing. Recordings are pruned after `RETAINED_AUDIO_MAX_AGE_DAYS` (30). A retained `.webm` with no sidecar predates this and needs `python -m notetaker.reprocess` by hand. Emails that cannot be sent are spooled and retried the same way.

## Troubleshooting

**Bot doesn't join calls unless it is added as a participant**: Make sure the bot user is added as a participant in the Talk room.

**"Could not connect" during setup**: Verify the Nextcloud URL is correct and starts with `https://`.

**"401 Unauthorized"**: Wrong bot username or password.

**"404 Not Found" on Talk API**: The Nextcloud Talk app may not be installed.

**No email notifications**: The bot needs subadmin status for user groups to look up email addresses. See setup instructions above.

**Container keeps restarting / setup says startup checks failed**: The notetaker checks Nextcloud, the notes folder, Gemini, SMTP (only when `SMTP_HOST` is set) and its state directory at startup and refuses to run if one fails. `docker compose logs notetaker | grep PREFLIGHT` names the one that failed; fix that value in `.env` and run `docker compose up -d`.

**Mail server on the same machine**: use `SMTP_HOST=host.docker.internal`, not `localhost` — inside the container `localhost` is the container.

**Transcription quality**: Gemini may mishear proper nouns. This is inherent to speech-to-text. For important terms, check the notes after generation.

**`PREFLIGHT FAILED whisper: ... not downloaded`**: run `docker compose run --rm --no-deps notetaker download-model`, then `docker compose up -d`. Or set `TRANSCRIPTION_BACKEND=gemini`.

**`PREFLIGHT FAILED whisper: ... memory limit`**: the container is capped below 4 GB. Set `NOTETAKER_MEMORY_LIMIT=6g` in `.env` and run `docker compose up -d`.

**`download-model` starts the bot instead of downloading**: the image predates the Whisper backend. Run `docker compose build` first, then the download command.

**Notes take 45–60 minutes after a one-hour call**: expected with Whisper on a CPU. If that is too slow, switch to Gemini (see Transcription).

**Upgrading**: `git pull`, then `docker compose up -d --build`. If you hand-edited `docker-compose.yml` (for example the `extra_hosts` pin), use `git stash && git pull && git stash pop` first. The image build now downloads about 300 MB more of Python packages than before.

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
