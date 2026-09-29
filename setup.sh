#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()    { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
success() { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
error()   { printf "${RED}[ERROR]${NC} %s\n" "$*"; }
header()  { printf "\n${BOLD}${CYAN}── %s ──${NC}\n\n" "$*"; }

prompt_value() {
    local prompt="$1" default="${2:-}"
    if [ -n "$default" ]; then
        printf "${BOLD}%s${NC} [%s]: " "$prompt" "$default" >&2
    else
        printf "${BOLD}%s${NC}: " "$prompt" >&2
    fi
    read -r value
    echo "${value:-$default}"
}

prompt_secret() {
    local prompt="$1"
    printf "${BOLD}%s${NC}: " "$prompt" >&2
    read -rs value
    echo >&2
    echo "$value"
}

prompt_yn() {
    local prompt="$1" default="${2:-n}"
    if [ "$default" = "y" ]; then
        printf "${BOLD}%s${NC} [Y/n]: " "$prompt"
    else
        printf "${BOLD}%s${NC} [y/N]: " "$prompt"
    fi
    read -r answer
    answer="${answer:-$default}"
    case "$answer" in
        [Yy]*) return 0 ;;
        *)     return 1 ;;
    esac
}

# ─── Banner ───────────────────────────────────────────────────────────────────
printf "${BOLD}${CYAN}"
cat << 'BANNER'

  _   _       _       _        _
 | \ | | ___ | |_ ___| |_ __ _| | _____ _ __
 |  \| |/ _ \| __/ _ \ __/ _` | |/ / _ \ '__|
 | |\  | (_) | ||  __/ || (_| |   <  __/ |
 |_| \_|\___/ \__\___|\__\__,_|_|\_\___|_|

  Nextcloud Talk AI Notetaker — Setup Wizard

  Press Enter to accept the answer shown in [brackets] (for yes/no questions, the capital letter).

BANNER
printf "${NC}"

# ─── Step 1: Prerequisites ────────────────────────────────────────────────────
header "Step 1: Prerequisites"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_NAME="${ID:-unknown}"
    OS_PRETTY="${PRETTY_NAME:-$OS_NAME}"
else
    OS_NAME="unknown"
    OS_PRETTY="Unknown Linux"
fi
info "Detected OS: $OS_PRETTY"

# Check Docker (or Podman compatibility)
if command -v docker >/dev/null 2>&1; then
    # Docker CLI found, but can it reach a daemon?
    if ! docker info >/dev/null 2>&1; then
        # No daemon — check for Podman socket
        PODMAN_SOCK="/run/user/$(id -u)/podman/podman.sock"
        if [ -S "$PODMAN_SOCK" ]; then
            export DOCKER_HOST="unix://$PODMAN_SOCK"
            if docker info >/dev/null 2>&1; then
                success "Docker CLI connected via Podman socket"
            else
                error "Docker CLI found but cannot connect to Podman."
                info "Try: export DOCKER_HOST=unix://$PODMAN_SOCK"
                exit 1
            fi
        elif command -v podman >/dev/null 2>&1; then
            error "Docker CLI found but no daemon running. Podman is installed but socket not found."
            info "Start Podman socket: systemctl --user enable --now podman.socket"
            info "Then: export DOCKER_HOST=unix://$PODMAN_SOCK"
            exit 1
        else
            error "Docker CLI found but the Docker daemon is not running."
            info "Start it with: sudo systemctl start docker"
            exit 1
        fi
    else
        DOCKER_VERSION=$(docker --version 2>/dev/null || echo "unknown")
        success "Docker is installed: $DOCKER_VERSION"
    fi
else
    error "Docker is not installed."
    echo
    info "Install Docker Engine: https://docs.docker.com/engine/install/"
    echo
    exit 1
fi

# Check Docker Compose
COMPOSE_CMD=""
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    success "Docker Compose v2 available"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    success "Docker Compose v1 available"
else
    error "Docker Compose is not installed."
    info "Docker Compose is included with Docker Engine from https://docs.docker.com/engine/install/"
    info "If you installed Docker via your distro's package manager, also install the compose plugin:"
    info "  https://docs.docker.com/compose/install/linux/"
    exit 1
fi

# Every connectivity test below is a curl call.
if command -v curl >/dev/null 2>&1; then
    success "curl is available"
else
    error "curl is not installed. Install it (apt install curl / dnf install curl) and re-run."
    exit 1
fi

# ─── Step 2: Nextcloud Connection ─────────────────────────────────────────────
header "Step 2: Nextcloud Connection"

echo "The notetaker needs its own Nextcloud user account."
echo "Before continuing, go to your Nextcloud admin panel and create"
echo "a new user for the bot (for example: ai-notetaker)."
echo

if ! prompt_yn "Have you created a bot user account in Nextcloud?"; then
    echo
    info "Create the bot user first, then re-run this script."
    exit 0
fi
echo

NC_URL=$(prompt_value "Enter your Nextcloud URL (e.g., https://cloud.example.com)")
# Strip trailing slash
NC_URL="${NC_URL%/}"

NC_USER=$(prompt_value "Enter the bot's username")
NC_PASS=$(prompt_secret "Enter the bot's password")

# Test API connectivity (use original URL from the host)
TEST_URL="$NC_URL"
info "Testing connection to $TEST_URL..."
HTTP_CODE=$(curl -s -o /tmp/nc-setup-test.json -w "%{http_code}" \
    -u "$NC_USER:$NC_PASS" \
    -H "OCS-APIRequest: true" \
    -H "Accept: application/json" \
    "$TEST_URL/ocs/v2.php/apps/spreed/api/v4/room" 2>/dev/null || echo "000")

case "$HTTP_CODE" in
    200)
        success "Connected to Nextcloud Talk API successfully!"
        # Try to get Nextcloud version
        NC_VER=$(curl -s -u "$NC_USER:$NC_PASS" \
            -H "OCS-APIRequest: true" -H "Accept: application/json" \
            "$TEST_URL/ocs/v1.php/cloud/capabilities" 2>/dev/null \
            | grep -o '"major":[0-9]*' | head -1 | cut -d: -f2 || echo "")
        if [ -n "$NC_VER" ]; then
            info "Nextcloud version: $NC_VER"
        fi
        ;;
    401)
        error "Authentication failed (401). Check the username and password."
        exit 1
        ;;
    404)
        error "Talk API not found (404). Make sure Nextcloud Talk is installed."
        info "Install it from the Nextcloud App Store: Settings > Apps > Talk"
        exit 1
        ;;
    000)
        error "Could not connect to $NC_URL"
        info "Check the URL — make sure it starts with https:// and is reachable."
        exit 1
        ;;
    *)
        error "Unexpected response (HTTP $HTTP_CODE)"
        info "Check the URL and try again."
        exit 1
        ;;
esac
rm -f /tmp/nc-setup-test.json

# Rewrite localhost URLs for Docker container networking
# localhost inside a container refers to the container itself, not the host
case "$NC_URL" in
    http://localhost:*|http://localhost|https://localhost:*|https://localhost)
        DOCKER_URL=$(echo "$NC_URL" | sed 's|://localhost|://host.docker.internal|')
        warn "The notetaker runs inside a Docker container where 'localhost'"
        warn "refers to the container, not your machine."
        info "Automatically rewriting to: $DOCKER_URL"
        NC_URL="$DOCKER_URL"
        ;;
esac

echo
AUTH_METHOD="nextcloud"
NC_WEB_PASS=""
if prompt_yn "Does your Nextcloud use Yunohost SSO?"; then
    AUTH_METHOD="yunohost"
    NC_WEB_PASS=$(prompt_secret "Enter the SSO web password (for browser login)")
fi

echo
info "Tip: To generate notes for a call, add the bot user as a"
info "participant in that Talk room before or during the call."

# ─── Step 3: Transcription ────────────────────────────────────────────────────
header "Step 3: Transcription"

cat << 'EOF'
After each call, the recording is first turned into written text (this is
called transcription). Gemini then turns that text into meeting notes.
Choose who does the transcription:

  Whisper — runs on this computer. Free, no daily limit, and the
    recording never leaves your server. Needs a fairly fast computer and
    a one-time 1.6 GB download (done for you at the end of this setup).
    Notes arrive 45-60 minutes after a one-hour call, depending on the CPU;
    the bot cannot record another call until it is done.
  Gemini — the recording is sent to Google, which turns it into text.
    Works on any computer, but Google's free plan covers about 3 one-hour
    calls a day, and notes can be delayed when Google is busy.

You can change this later (see README, "Transcription").

EOF

# Hardware probes. Each falls back to 0 ("could not read") instead of
# aborting the wizard under set -e. The SETUP_* overrides exist for tests;
# an empty override means "could not read".
CPU_THREADS="${SETUP_CPU_THREADS-$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)}"
MEM_GB="${SETUP_MEM_GB-$(awk '/^MemTotal:/ {printf "%.0f", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)}"
MEM_AVAIL_GB="${SETUP_MEM_AVAIL_GB-$(awk '/^MemAvailable:/ {printf "%.0f", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)}"
DISK_GB="${SETUP_DISK_GB-$(df -Pk . 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}' || echo 0)}"
ARCH="${SETUP_ARCH-$(uname -m 2>/dev/null || echo unknown)}"
# AVX2: 1 = present, 0 = absent, "" = could not read (no /proc/cpuinfo).
if [ -n "${SETUP_AVX2+x}" ]; then
    AVX2="$SETUP_AVX2"
elif [ ! -r /proc/cpuinfo ]; then
    AVX2=""
elif grep -qw avx2 /proc/cpuinfo 2>/dev/null; then
    AVX2=1
else
    AVX2=0
fi
# Anything that is not a whole number counts as unreadable.
for v in CPU_THREADS MEM_GB MEM_AVAIL_GB DISK_GB; do
    case "${!v}" in
        ''|*[!0-9]*) printf -v "$v" '%s' 0 ;;
    esac
done

describe_probe() { # value  unit-words  what-it-is
    if [ "$1" -gt 0 ]; then printf '%s %s' "$1" "$2"; else printf 'could not read the %s' "$3"; fi
}
CHECK_LINE="$(describe_probe "$CPU_THREADS" "processor cores" "processor count")"
CHECK_LINE="$CHECK_LINE, $(describe_probe "$MEM_GB" "GB memory" "memory")"
[ "$MEM_GB" -gt 0 ] && [ "$MEM_AVAIL_GB" -gt 0 ] && CHECK_LINE="$CHECK_LINE ($MEM_AVAIL_GB GB free)"
CHECK_LINE="$CHECK_LINE, $(describe_probe "$DISK_GB" "GB free disk" "free disk")"
info "Checking this computer... $CHECK_LINE."

REASONS=()
UNKNOWN=""
if [ "$CPU_THREADS" -le 0 ]; then UNKNOWN=1
elif [ "$CPU_THREADS" -lt 4 ]; then REASONS+=("only $CPU_THREADS processor cores (Whisper needs 4)"); fi
if [ "$MEM_GB" -le 0 ]; then UNKNOWN=1
elif [ "$MEM_GB" -lt 8 ]; then REASONS+=("$MEM_GB GB memory (Whisper needs 8)"); fi
if [ "$MEM_AVAIL_GB" -gt 0 ] && [ "$MEM_AVAIL_GB" -lt 4 ]; then
    REASONS+=("only $MEM_AVAIL_GB GB memory free right now (Whisper needs 4)")
fi
if [ "$DISK_GB" -le 0 ]; then UNKNOWN=1
elif [ "$DISK_GB" -lt 5 ]; then REASONS+=("$DISK_GB GB free disk (Whisper needs 5)"); fi
if [ "$ARCH" = "unknown" ] || [ -z "$AVX2" ]; then UNKNOWN=1
elif [ "$ARCH" != "x86_64" ] || [ "$AVX2" != "1" ]; then
    REASONS+=("this CPU is slow at this kind of work (no AVX2 / ARM), so a 1-hour call could take hours")
fi

if [ "${#REASONS[@]}" -gt 0 ]; then
    MSG="${REASONS[0]}"
    for r in "${REASONS[@]:1}"; do MSG="$MSG and $r"; done
    warn "${MSG^}. Gemini is recommended for this computer."
    WHISPER_DEFAULT="n"
elif [ -n "$UNKNOWN" ]; then
    warn "Gemini is the safe choice when the hardware is unknown."
    WHISPER_DEFAULT="n"
else
    success "This computer can run Whisper (recommended)."
    WHISPER_DEFAULT="y"
fi
echo

if prompt_yn "Use Whisper for transcription?" "$WHISPER_DEFAULT"; then
    TRANSCRIPTION_BACKEND="whisper"
    MEMORY_LIMIT="6g"
    success "Whisper will transcribe on this computer."
    if [ "$WHISPER_DEFAULT" = "n" ]; then
        warn "Whisper may be slow or run out of memory here. If notes stop arriving, switch to Gemini (README, \"Transcription\")."
    fi
else
    TRANSCRIPTION_BACKEND="gemini"
    MEMORY_LIMIT="2g"
    success "Gemini will transcribe."
fi

# ─── Step 4: Gemini API Key ──────────────────────────────────────────────────
header "Step 4: Gemini API Key"

cat << 'EOF'
Gemini is Google's AI service. The notetaker needs an API key — a long
password that lets it use Gemini through your Google account.
Get a free one at: https://aistudio.google.com/apikey
(Sign in, click "Create API key", copy it, paste it below. Typing is hidden.)
EOF
if [ "$TRANSCRIPTION_BACKEND" = "whisper" ]; then
    echo "Gemini only writes the notes from text, so the free plan is plenty."
else
    echo "Gemini also transcribes the audio. The free plan covers about 3 one-hour calls a day."
    echo "For more, turn on billing in Google AI Studio (about \$0.02 per one-hour call)."
fi
echo

GEMINI_KEY=$(prompt_secret "Enter your Gemini API key")

# Test Gemini API
info "Testing Gemini API key..."
GEMINI_RESP=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_KEY" 2>/dev/null || echo "000")

case "$GEMINI_RESP" in
    200)
        success "Gemini API key is valid!" ;;
    400|403)
        error "Gemini API key is invalid or unauthorized."
        exit 1 ;;
    000)
        error "Could not reach the Gemini API. Check your internet connection."
        exit 1 ;;
    *)
        warn "Unexpected response ($GEMINI_RESP) from Gemini API. Continuing anyway." ;;
esac

# ─── Step 5: Notes Storage ────────────────────────────────────────────────────
header "Step 5: Notes Storage"

echo "Where should meeting notes be saved?"
echo
echo "  A) Nextcloud (recommended) — save to a shared folder in Nextcloud"
echo "  B) Local folder — save .md files to a directory on this machine"
echo

STORAGE_CHOICE=$(prompt_value "Choose A or B" "A")
STORAGE_CHOICE=$(echo "$STORAGE_CHOICE" | tr '[:lower:]' '[:upper:]')

NOTES_STORAGE="nextcloud"
NOTES_FOLDER="/meeting-notes"
LOCAL_NOTES_DIR=""

if [ "$STORAGE_CHOICE" = "B" ]; then
    NOTES_STORAGE="local"
    # This path is INSIDE the container. docker-compose.yml mounts ./notes
    # (next to this script) at /data/notes; nothing else persists across a
    # restart. Before 2026-09-14 the wizard wrote a host path here and the
    # notes silently went to /app/notes inside the container.
    LOCAL_NOTES_DIR="/data/notes"
    mkdir -p ./notes
    success "Notes will be written to ./notes (mounted as $LOCAL_NOTES_DIR in the container)"
else
    echo
    echo "The bot saves notes to a folder in its own Nextcloud file space."
    echo "This can be any folder name you choose (e.g., 'notes', 'calls', etc.)."
    echo
    echo "To let other users see the notes:"
    echo "  1. Create a folder with any name in any user's Nextcloud"
    echo "  2. Share that folder with the bot user (${NC_USER})"
    echo "  3. Enter that folder's name below exactly as it appears to the bot"
    echo
    echo "If you skip sharing, the bot creates the folder in its own space"
    echo "(only the bot will be able to see it)."
    echo
    NOTES_FOLDER=$(prompt_value "Folder name" "meeting-notes")
    # Ensure leading slash
    case "$NOTES_FOLDER" in
        /*) ;;
        *)  NOTES_FOLDER="/$NOTES_FOLDER" ;;
    esac

    # Test WebDAV access
    info "Testing WebDAV access to $NOTES_FOLDER..."
    DAV_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -u "$NC_USER:$NC_PASS" \
        -X PROPFIND \
        "$TEST_URL/remote.php/dav/files/$NC_USER$NOTES_FOLDER" 2>/dev/null || echo "000")

    case "$DAV_CODE" in
        207)
            success "Bot can access $NOTES_FOLDER" ;;
        404)
            warn "Folder $NOTES_FOLDER not found. The bot will create it on first use." ;;
        *)
            warn "Could not verify folder access (HTTP $DAV_CODE)."
            info "Make sure you share the folder with the bot user in Nextcloud." ;;
    esac
fi

# ─── Step 6: Email (optional) ────────────────────────────────────────────────
header "Step 6: Email Notifications (optional)"

SMTP_HOST=""
SMTP_PORT=""
SMTP_FROM=""
SMTP_USER=""
SMTP_PASS=""
EMAIL_OVERRIDES=""

if prompt_yn "Send email notifications after each call?"; then
    echo
    echo "The notetaker runs in a container, so 'localhost' would be the container"
    echo "itself. A mail server on this machine is reached as host.docker.internal."
    SMTP_HOST=$(prompt_value "SMTP host" "host.docker.internal")
    case "$SMTP_HOST" in
        localhost|127.0.0.1|::1)
            warn "Rewriting SMTP host '$SMTP_HOST' to host.docker.internal (the container's name for this machine)"
            SMTP_HOST="host.docker.internal"
            ;;
    esac
    SMTP_PORT=$(prompt_value "SMTP port" "587")
    SMTP_FROM=$(prompt_value "From address (e.g., notetaker@example.com)")
    if prompt_yn "Does the SMTP server require authentication?"; then
        SMTP_USER=$(prompt_value "SMTP username")
        SMTP_PASS=$(prompt_secret "SMTP password")
    fi

    echo
    info "Email lookup: The bot looks up participant email addresses from Nextcloud."
    info "For this to work, the bot needs to be a 'subadmin' of user groups."
    info "(This is NOT the same as admin — it only gives read access to emails.)"
    info "See the README for setup instructions."
    echo

    if prompt_yn "Do you have email overrides? (e.g., user=email@example.com)"; then
        EMAIL_OVERRIDES=$(prompt_value "Email overrides (comma-separated user=email pairs)")
    fi

    success "Email notifications configured."
else
    info "Email notifications disabled. Notes will still be saved."
fi

# ─── Step 7: Generate .env and start ─────────────────────────────────────────
header "Step 7: Starting the Notetaker"

if [ -f .env ]; then
    ENV_BACKUP=".env.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$ENV_BACKUP"
    chmod 600 "$ENV_BACKUP"
    warn "Existing .env saved as $ENV_BACKUP"
fi

# Declined email: document the keys but leave them commented out. An unset
# SMTP_HOST is what switches email off in the app; python-dotenv would
# otherwise set the keys to empty strings.
if [ -n "$SMTP_HOST" ]; then
    SMTP_PREFIX=""
else
    SMTP_PREFIX="# "
fi

# Empty web password: leave it unset so config.py falls back to
# NEXTCLOUD_PASSWORD instead of using an empty string verbatim.
if [ -n "$NC_WEB_PASS" ]; then
    WEB_PASS_PREFIX=""
else
    WEB_PASS_PREFIX="# "
fi

info "Writing .env file..."
umask 077
cat > .env << ENVFILE
# Nextcloud connection
NEXTCLOUD_URL=$NC_URL
NEXTCLOUD_USER=$NC_USER
NEXTCLOUD_PASSWORD=$NC_PASS
# Browser login password (only needed for Yunohost SSO, defaults to NEXTCLOUD_PASSWORD)
${WEB_PASS_PREFIX}NEXTCLOUD_WEB_PASSWORD=$NC_WEB_PASS

# Authentication method: "nextcloud" (standard) or "yunohost" (SSO)
AUTH_METHOD=$AUTH_METHOD

# Transcription: "whisper" (runs on this machine, private) or "gemini" (Google hears the audio).
# See README, "Transcription", before changing this: switching to Whisper needs the model download first.
TRANSCRIPTION_BACKEND=$TRANSCRIPTION_BACKEND
# Whisper model (only when TRANSCRIPTION_BACKEND=whisper). large-v3-turbo is the default;
# small is about twice as fast and less accurate.
WHISPER_MODEL=large-v3-turbo
# Language of the calls as a two-letter code. Leave empty to auto-detect.
WHISPER_LANGUAGE=en
# Where the model files live INSIDE the container. docker-compose.yml mounts ./models here.
WHISPER_MODEL_DIR=/data/models
# Memory cap for the container. Whisper needs 6g; Gemini-only installs keep 2g.
NOTETAKER_MEMORY_LIMIT=$MEMORY_LIMIT

# Gemini AI (writes the notes; with TRANSCRIPTION_BACKEND=gemini it also transcribes the audio)
GEMINI_API_KEY=$GEMINI_KEY
# Gemini model for the notes (and for transcription when the backend is gemini)
GEMINI_MODEL=gemini-2.5-flash-lite

# Who writes the notes from the Whisper transcript: "gemini", or "claude"
# (Claude Code on a Claude subscription; nothing goes to Google). See README.
NOTES_BACKEND=gemini
CLAUDE_MODEL=haiku
# Long-lived subscription token from \`claude setup-token\` (NOTES_BACKEND=claude only)
# CLAUDE_CODE_OAUTH_TOKEN=

# Notes storage: "nextcloud" (upload via WebDAV) or "local" (save to filesystem)
NOTES_STORAGE=$NOTES_STORAGE
# Nextcloud folder path for notes (used when NOTES_STORAGE=nextcloud)
NOTES_FOLDER=$NOTES_FOLDER
# Directory INSIDE the container for notes (used when NOTES_STORAGE=local).
# docker-compose.yml mounts the notes/ folder beside it here; nothing else persists across restarts.
LOCAL_NOTES_DIR=$LOCAL_NOTES_DIR

# Optional: keep the word-for-word Whisper transcript of every call as well.
# It is never emailed. Use a top-level folder shared with the bot only: it
# may not be NOTES_FOLDER, inside or above it, or share a parent folder with
# it, since the notes folder is usually shared with the attendees.
# Empty or unset = transcripts are not kept.
# TRANSCRIPTS_FOLDER=/meeting-notes-transcriptions
# Same, for NOTES_STORAGE=local (keep it under /data/notes so it persists):
# LOCAL_TRANSCRIPTS_DIR=/data/notes/transcripts

# Before a recording is deleted, its output is checked (verify.py). A call
# over VERIFY_MIN_MINUTES long whose transcript has fewer than
# MIN_SPEECH_CHARS_PER_MINUTE characters of speech per minute, or whose notes
# are empty, is HELD: the audio is kept, nothing is emailed or shared, and
# ALERT_EMAIL is told. Release one by hand with reprocess.py --release.
# With TRANSCRIPTION_BACKEND=gemini, calls of 5-10 minutes are transcribed in one
# pass with no transcript to check against, so they are always held.
# MIN_SPEECH_CHARS_PER_MINUTE=30
# VERIFY_MIN_MINUTES=5
# ALERT_EMAIL=
# During a call: email ALERT_EMAIL when nobody has been heard for this many
# minutes while others are in the call, so a dead recording can be fixed
# while the call is still going. 0 turns it off.
# SILENCE_ALERT_MINUTES=5

# Optional: user ID to always place last in note filenames
FILENAME_LAST_USER=

# SMTP (for emailing notes to participants). The container reaches a mail
# server on this machine as host.docker.internal, never localhost.
${SMTP_PREFIX}SMTP_HOST=$SMTP_HOST
${SMTP_PREFIX}SMTP_PORT=$SMTP_PORT
${SMTP_PREFIX}SMTP_FROM=$SMTP_FROM
# Leave blank for unauthenticated local SMTP
${SMTP_PREFIX}SMTP_USER=$SMTP_USER
${SMTP_PREFIX}SMTP_PASSWORD=$SMTP_PASS

# Optional: comma-separated user=email overrides for email lookup
${SMTP_PREFIX}EMAIL_OVERRIDES=$EMAIL_OVERRIDES

# Optional: fallback email domain when lookup fails
MAIL_DOMAIN=

# TLS for SMTP: "auto" (default) verifies certificates for real hostnames and
# public IPs, and skips verification only for local hops (host.docker.internal,
# localhost, private/CGNAT addresses) that cannot present a matching
# certificate. "true"/"false" force it; any other value verifies.
SMTP_TLS_VERIFY=auto
# Connect/read timeout for SMTP. Without one, a dead mail host blocks the
# service indefinitely.
SMTP_TIMEOUT_SECONDS=30

# Service settings
POLL_INTERVAL_SECONDS=10
AUDIO_DIR=/tmp/notetaker-audio

# Persistent directory for the undelivered-email spool and for recordings
# retained after a pipeline failure. Must NOT be a tmpfs.
STATE_DIR=/data/notes
# Touched every poll; the container healthcheck reads its age.
HEARTBEAT_FILE=/tmp/notetaker-heartbeat
# Recordings retained after a failure are deleted after this many days.
RETAINED_AUDIO_MAX_AGE_DAYS=30
# Set to 1 to start even if startup dependency checks fail (not recommended).
# SKIP_PREFLIGHT=0

# RMS level above which a track counts as speech for speaker attribution.
# NOTETAKER_VAD_THRESHOLD=0.02
ENVFILE
chmod 600 .env
umask 022

success ".env file created (mode 600)."

# Both directories are bind mounts; create them as the current user so
# Docker does not create them root-owned in the checkout.
mkdir -p ./models ./notes

download_failed() {
    trap - INT
    echo
    error "The Whisper model download did not finish."
    info "Your settings are saved in .env. To finish, run these two commands:"
    echo "          $COMPOSE_CMD run --rm --no-deps notetaker download-model"
    echo "          $COMPOSE_CMD up -d"
    info "The download resumes where it stopped. To use Gemini instead: set"
    echo "        TRANSCRIPTION_BACKEND=gemini in .env, then run: $COMPOSE_CMD up -d"
    exit 1
}

info "Building the notetaker (a few minutes the first time)..."
echo
if [ "$TRANSCRIPTION_BACKEND" = "whisper" ]; then
    $COMPOSE_CMD build
    echo
    info "Downloading the Whisper model, the 1.6 GB file from Step 3 (one time). This takes a few minutes depending on your internet speed; progress is printed every few seconds..."
    trap download_failed INT
    if ! $COMPOSE_CMD run --rm --no-deps notetaker download-model; then
        download_failed
    fi
    trap - INT
    echo
    $COMPOSE_CMD up -d
else
    $COMPOSE_CMD up -d --build
fi

# The app checks every dependency at startup and refuses to run if one
# fails; the container then restarts in a loop. Wait for the healthcheck
# instead of assuming success.
HEALTH_POLL_INTERVAL="${SETUP_HEALTH_POLL_INTERVAL:-5}"
HEALTH_TIMEOUT="${SETUP_HEALTH_TIMEOUT:-180}"
info "Waiting for the notetaker to pass its startup checks (up to ${HEALTH_TIMEOUT}s)..."
CONTAINER_ID=$($COMPOSE_CMD ps -q notetaker 2>/dev/null || true)
CONTAINER_ID=${CONTAINER_ID%%$'\n'*}
if [ -z "$CONTAINER_ID" ]; then
    error "The notetaker container was not created. Output of '$COMPOSE_CMD up' is above."
    exit 1
fi

show_startup_log() {
    echo
    error "Startup checks did not pass. Last lines of the container log:"
    echo
    # PREFLIGHT lines name the dependency that failed; make them stand out.
    $COMPOSE_CMD logs --tail 40 notetaker 2>&1 | while IFS= read -r line; do
        case "$line" in
            *PREFLIGHT*|*"Refusing to start"*) printf "${RED}%s${NC}\n" "$line" ;;
            *)                                 printf "%s\n" "$line" ;;
        esac
    done || true
    echo
    info "Fix the setting it names in .env and run: $COMPOSE_CMD up -d"
}

WAITED=0
STATE=""
while [ "$WAITED" -le "$HEALTH_TIMEOUT" ]; do
    STATE=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_ID" 2>/dev/null || echo "missing none")
    case "$STATE" in
        "running healthy")
            break ;;
        "restarting "*|"exited "*|"dead "*|"missing "*|*" unhealthy")
            show_startup_log
            exit 1 ;;
    esac
    sleep "$HEALTH_POLL_INTERVAL"
    WAITED=$((WAITED + HEALTH_POLL_INTERVAL))
    # A zero interval (tests) must still advance the clock.
    [ "$HEALTH_POLL_INTERVAL" -eq 0 ] && WAITED=$((WAITED + 1))
done

if [ "$STATE" != "running healthy" ]; then
    error "The notetaker did not become healthy within ${HEALTH_TIMEOUT}s (last state: $STATE)."
    show_startup_log
    exit 1
fi

echo
success "Notetaker is running!"
echo
info "Next steps:"
echo "  1. Add the bot user ($NC_USER) to Talk rooms you want it to monitor"
if [ "$NOTES_STORAGE" = "nextcloud" ]; then
    echo "  2. Share the '$NOTES_FOLDER' folder with the bot in Nextcloud"
fi
echo "  3. Make the bot a 'subadmin' of user groups for email lookup (see README)"
echo
info "Useful commands:"
echo "  $COMPOSE_CMD logs -f        # View logs"
echo "  $COMPOSE_CMD up -d           # Apply .env changes (restart is not enough)"
echo "  $COMPOSE_CMD restart        # Restart"
echo "  $COMPOSE_CMD down           # Stop"
echo "  ./setup.sh                  # Re-configure"
echo
