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
        printf "${BOLD}%s${NC} [%s]: " "$prompt" "$default"
    else
        printf "${BOLD}%s${NC}: " "$prompt"
    fi
    read -r value
    echo "${value:-$default}"
}

prompt_secret() {
    local prompt="$1"
    printf "${BOLD}%s${NC}: " "$prompt"
    read -rs value
    echo
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

# Check Docker
if command -v docker >/dev/null 2>&1; then
    DOCKER_VERSION=$(docker --version 2>/dev/null || echo "unknown")
    success "Docker is installed: $DOCKER_VERSION"
else
    error "Docker is not installed."
    echo
    case "$OS_NAME" in
        ubuntu|debian)
            info "Install with: sudo apt install docker.io docker-compose-plugin" ;;
        fedora)
            info "Install with: sudo dnf install docker docker-compose-plugin" ;;
        arch|manjaro)
            info "Install with: sudo pacman -S docker docker-compose" ;;
        *)
            info "Install from: https://docs.docker.com/engine/install/" ;;
    esac
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
    case "$OS_NAME" in
        ubuntu|debian)
            info "Install with: sudo apt install docker-compose-plugin" ;;
        fedora)
            info "Install with: sudo dnf install docker-compose-plugin" ;;
        arch|manjaro)
            info "Install with: sudo pacman -S docker-compose" ;;
        *)
            info "Install from: https://docs.docker.com/compose/install/" ;;
    esac
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

# Test API connectivity
info "Testing connection to $NC_URL..."
HTTP_CODE=$(curl -s -o /tmp/nc-setup-test.json -w "%{http_code}" \
    -u "$NC_USER:$NC_PASS" \
    -H "OCS-APIRequest: true" \
    -H "Accept: application/json" \
    "$NC_URL/ocs/v2.php/apps/spreed/api/v4/room" 2>/dev/null || echo "000")

case "$HTTP_CODE" in
    200)
        success "Connected to Nextcloud Talk API successfully!"
        # Try to get Nextcloud version
        NC_VER=$(curl -s -u "$NC_USER:$NC_PASS" \
            -H "OCS-APIRequest: true" -H "Accept: application/json" \
            "$NC_URL/ocs/v1.php/cloud/capabilities" 2>/dev/null \
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

# ─── Step 3: Gemini API Key ──────────────────────────────────────────────────
header "Step 3: Gemini API Key"

echo "The notetaker uses Google's Gemini AI to transcribe and summarize calls."
echo "Get a free API key at: https://aistudio.google.com/apikey"
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

# ─── Step 4: Notes Storage ────────────────────────────────────────────────────
header "Step 4: Notes Storage"

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
    LOCAL_NOTES_DIR=$(prompt_value "Path for local notes" "./notes")
    mkdir -p "$LOCAL_NOTES_DIR"
    success "Local notes directory: $LOCAL_NOTES_DIR"
else
    echo
    echo "Create a folder in your Nextcloud (e.g., 'meeting-notes'), then share"
    echo "it with the bot user. The bot will save notes into this shared folder."
    echo
    NOTES_FOLDER=$(prompt_value "Nextcloud folder name" "meeting-notes")
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
        "$NC_URL/remote.php/dav/files/$NC_USER$NOTES_FOLDER" 2>/dev/null || echo "000")

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

# ─── Step 5: Email (optional) ────────────────────────────────────────────────
header "Step 5: Email Notifications (optional)"

SMTP_HOST=""
SMTP_PORT=""
SMTP_FROM=""
SMTP_USER=""
SMTP_PASS=""
EMAIL_OVERRIDES=""

if prompt_yn "Send email notifications after each call?"; then
    echo
    SMTP_HOST=$(prompt_value "SMTP host" "localhost")
    SMTP_PORT=$(prompt_value "SMTP port" "25")
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

# ─── Step 6: Generate .env and start ─────────────────────────────────────────
header "Step 6: Starting the Notetaker"

info "Writing .env file..."

cat > .env << ENVFILE
# Nextcloud connection
NEXTCLOUD_URL=$NC_URL
NEXTCLOUD_USER=$NC_USER
NEXTCLOUD_PASSWORD=$NC_PASS
ENVFILE

if [ -n "$NC_WEB_PASS" ]; then
    echo "NEXTCLOUD_WEB_PASSWORD=$NC_WEB_PASS" >> .env
fi

cat >> .env << ENVFILE

# Authentication method: "nextcloud" (standard) or "yunohost" (SSO)
AUTH_METHOD=$AUTH_METHOD

# Gemini AI
GEMINI_API_KEY=$GEMINI_KEY
GEMINI_MODEL=gemini-2.5-flash-lite

# Notes storage: "nextcloud" or "local"
NOTES_STORAGE=$NOTES_STORAGE
NOTES_FOLDER=$NOTES_FOLDER
LOCAL_NOTES_DIR=$LOCAL_NOTES_DIR

# Service settings
POLL_INTERVAL_SECONDS=10
AUDIO_DIR=/tmp/notetaker-audio
ENVFILE

if [ -n "$SMTP_HOST" ]; then
    cat >> .env << ENVFILE

# Email notifications
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_FROM=$SMTP_FROM
SMTP_USER=$SMTP_USER
SMTP_PASSWORD=$SMTP_PASS
EMAIL_OVERRIDES=$EMAIL_OVERRIDES
ENVFILE
fi

success ".env file created."

info "Building and starting the notetaker..."
echo
$COMPOSE_CMD up -d --build

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
echo "  $COMPOSE_CMD restart        # Restart"
echo "  $COMPOSE_CMD down           # Stop"
echo "  ./setup.sh                  # Re-configure"
echo
