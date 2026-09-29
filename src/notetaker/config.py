from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

from notetaker.mailer import TLS_VERIFY_AUTO, TLS_VERIFY_FALSE, TLS_VERIFY_TRUE

_TLS_VERIFY_ACCEPTED = TLS_VERIFY_TRUE | TLS_VERIFY_FALSE | {TLS_VERIFY_AUTO}


def _normalise(value: str | None) -> str:
    return (value or "").strip().lower()


def _path_parts(path: str) -> list[str]:
    """Folder path as comparable segments. Casefolded so a near-miss like
    /Meeting-Notes/x is still caught as inside /meeting-notes."""
    normalised = os.path.normpath("/" + (path or "").strip()).replace("\\", "/")
    return [p.casefold() for p in normalised.split("/") if p]


def _parse_email_overrides(raw: str) -> dict[str, str]:
    """Parse 'user1=email1,user2=email2' into a dict."""
    overrides = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            uid, email = pair.split("=", 1)
            overrides[uid.strip()] = email.strip()
    return overrides


@dataclass(frozen=True)
class Config:
    nextcloud_url: str
    nextcloud_user: str
    nextcloud_password: str
    nextcloud_web_password: str
    gemini_api_key: str
    auth_method: str = "nextcloud"
    gemini_model: str = "gemini-2.5-flash-lite"
    # "gemini" sends the audio to Google (six requests per hour-long call on
    # the chunked path); "whisper" transcribes locally and sends Gemini one
    # text request. Gemini always writes the notes, so the key stays required.
    transcription_backend: str = "gemini"
    # Who writes the notes from a Whisper transcript. "gemini" uses the API
    # key above; "claude" runs Claude Code headless on a subscription
    # (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`) and never sends
    # anything to Google, so GEMINI_API_KEY becomes optional.
    notes_backend: str = "gemini"
    claude_model: str = "haiku"
    claude_oauth_token: str = ""
    whisper_model: str = "large-v3-turbo"
    whisper_model_dir: str = "/data/models"
    # Two-letter code; empty means auto-detect (decided from the first 30 s
    # of speech, which a noisy opening can get wrong for the whole hour).
    whisper_language: str = "en"
    filename_last_user: str = ""
    notes_storage: str = "nextcloud"
    local_notes_dir: str = ""
    # Where the word-for-word transcript goes. Empty means it is not kept.
    # It is never emailed, so this folder must not be one the attendees can
    # see — which is why it may not be the notes folder or inside it.
    transcripts_folder: str = ""
    local_transcripts_dir: str = ""
    email_overrides: dict[str, str] = field(default_factory=dict)
    mail_domain: str = ""
    # Email is on only when SMTP_HOST is present in the environment. The
    # wizard writes the SMTP keys commented out when email is declined; with
    # nothing to disable, preflight used to probe localhost:25 inside the
    # container and crash-loop the default install.
    email_enabled: bool = True
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_from: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_timeout: int = 30
    # "auto" verifies certificates for real hostnames and public IPs, and
    # skips verification only for local hops (host.docker.internal, localhost,
    # private/CGNAT addresses) that cannot present a matching certificate.
    # "true"/"false" force it; any unrecognised value verifies.
    smtp_tls_verify: str = "auto"
    # Delete retained audio older than this so a long outage can't fill the disk.
    retained_audio_max_age_days: int = 30
    poll_interval: int = 10
    notes_folder: str = "/Talk/Notes"
    audio_dir: str = "/tmp/notetaker-audio"
    # Persistent (non-tmpfs) directory for the undelivered-email spool and
    # for audio retained after a pipeline failure.
    state_dir: str = "/data/notes"
    heartbeat_file: str = "/tmp/notetaker-heartbeat"

    @property
    def spool_dir(self) -> str:
        return os.path.join(self.state_dir, "failed-email")

    @property
    def retained_audio_dir(self) -> str:
        return os.path.join(self.state_dir, "retained-audio")

    def validate(self) -> list[str]:
        """Return a list of configuration problems. Empty means coherent.

        These are pure checks — no network. Reachability is covered by
        preflight.run_preflight().
        """
        problems: list[str] = []

        if not self.nextcloud_url.startswith(("http://", "https://")):
            problems.append(
                f"NEXTCLOUD_URL must start with http:// or https:// (got {self.nextcloud_url!r})"
            )
        if self.email_enabled:
            problems.extend(self._validate_smtp())
        if self.notes_storage not in ("nextcloud", "local"):
            problems.append(
                f"NOTES_STORAGE must be 'nextcloud' or 'local' (got {self.notes_storage!r})"
            )
        if self.notes_storage == "local" and not self.local_notes_dir:
            problems.append("NOTES_STORAGE=local requires LOCAL_NOTES_DIR")
        problems.extend(self._validate_transcripts())
        if self.auth_method not in ("nextcloud", "yunohost"):
            problems.append(
                f"AUTH_METHOD must be 'nextcloud' or 'yunohost' (got {self.auth_method!r})"
            )
        if self.transcription_backend not in ("gemini", "whisper"):
            problems.append(
                "TRANSCRIPTION_BACKEND must be 'gemini' or 'whisper' "
                f"(got {self.transcription_backend!r})"
            )
        if self.notes_backend not in ("gemini", "claude"):
            problems.append(
                f"NOTES_BACKEND must be 'gemini' or 'claude' (got {self.notes_backend!r})"
            )
        if self.notes_backend == "claude":
            if not self.claude_oauth_token:
                problems.append(
                    "NOTES_BACKEND=claude requires CLAUDE_CODE_OAUTH_TOKEN "
                    "(run `claude setup-token` on any machine logged in to the subscription)"
                )
            if self.transcription_backend != "whisper":
                problems.append(
                    "NOTES_BACKEND=claude requires TRANSCRIPTION_BACKEND=whisper "
                    "(Claude cannot transcribe audio)"
                )
        elif not self.gemini_api_key:
            problems.append("GEMINI_API_KEY is required unless NOTES_BACKEND=claude")
        if self.poll_interval < 1:
            problems.append("POLL_INTERVAL_SECONDS must be >= 1")
        return problems

    @property
    def transcripts_enabled(self) -> bool:
        if self.notes_storage == "local":
            return bool(self.local_transcripts_dir)
        return bool(self.transcripts_folder)

    def _validate_transcripts(self) -> list[str]:
        """In Nextcloud the notes folder is shared with the attendees, so the
        transcript must never land in it, directly or in a subfolder. A local
        notes directory is shared with nobody; there it only needs to differ,
        or a transcript would overwrite the notes of the same name."""
        if self.notes_storage == "local":
            if not self.local_transcripts_dir:
                return []
            if _path_parts(self.local_transcripts_dir) == _path_parts(
                self.local_notes_dir
            ):
                return [
                    (
                        "LOCAL_TRANSCRIPTS_DIR must differ from LOCAL_NOTES_DIR "
                        f"(both are {self.local_notes_dir!r})"
                    )
                ]
            return []

        if not self.transcripts_folder:
            return []
        notes_parts = _path_parts(self.notes_folder)
        transcripts_parts = _path_parts(self.transcripts_folder)
        if not transcripts_parts:
            return [
                (
                    "TRANSCRIPTS_FOLDER must name a folder, not the root "
                    f"(got {self.transcripts_folder!r})"
                )
            ]
        if transcripts_parts[: len(notes_parts)] == notes_parts:
            return [
                (
                    f"TRANSCRIPTS_FOLDER ({self.transcripts_folder}) must not be "
                    f"NOTES_FOLDER ({self.notes_folder}) or inside it: the notes "
                    "folder is shared with the attendees and transcripts are private"
                )
            ]
        return []

    def _validate_smtp(self) -> list[str]:
        """SMTP checks; only meaningful when email is enabled."""
        problems: list[str] = []
        if not 1 <= self.smtp_port <= 65535:
            problems.append(f"SMTP_PORT {self.smtp_port} is not a valid port")
        if bool(self.smtp_user) != bool(self.smtp_password):
            problems.append(
                "SMTP_USER and SMTP_PASSWORD must be set together (or both left blank)"
            )
        if self.smtp_from and "@" not in self.smtp_from:
            problems.append(f"SMTP_FROM is not an email address: {self.smtp_from!r}")
        if _normalise(self.smtp_tls_verify) not in _TLS_VERIFY_ACCEPTED:
            problems.append(
                f"SMTP_TLS_VERIFY must be auto, true or false (got {self.smtp_tls_verify!r})"
            )

        # A bare IP for SMTP_HOST is what caused months of silent email loss:
        # the server's Tailscale address changed and nothing noticed.
        if re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", self.smtp_host or ""):
            problems.append(
                f"SMTP_HOST is a hardcoded IP ({self.smtp_host}) — it will go stale "
                "silently. Prefer a hostname such as host.docker.internal."
            )
        return problems

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        required = [
            "NEXTCLOUD_URL",
            "NEXTCLOUD_USER",
            "NEXTCLOUD_PASSWORD",
        ]
        for var in required:
            if not os.environ.get(var):
                raise ValueError(f"Missing required environment variable: {var}")

        return cls(
            nextcloud_url=os.environ["NEXTCLOUD_URL"].rstrip("/"),
            nextcloud_user=os.environ["NEXTCLOUD_USER"],
            nextcloud_password=os.environ["NEXTCLOUD_PASSWORD"],
            nextcloud_web_password=os.getenv(
                "NEXTCLOUD_WEB_PASSWORD", os.environ["NEXTCLOUD_PASSWORD"]
            ),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            auth_method=os.getenv("AUTH_METHOD", "nextcloud"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            transcription_backend=os.getenv("TRANSCRIPTION_BACKEND", "gemini")
            .strip()
            .lower(),
            notes_backend=os.getenv("NOTES_BACKEND", "gemini").strip().lower(),
            claude_model=os.getenv("CLAUDE_MODEL", "haiku").strip() or "haiku",
            claude_oauth_token=os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3-turbo").strip()
            or "large-v3-turbo",
            whisper_model_dir=os.getenv("WHISPER_MODEL_DIR", "/data/models"),
            whisper_language=os.getenv("WHISPER_LANGUAGE", "en").strip(),
            filename_last_user=os.getenv("FILENAME_LAST_USER", ""),
            notes_storage=os.getenv("NOTES_STORAGE", "nextcloud"),
            local_notes_dir=os.getenv("LOCAL_NOTES_DIR", ""),
            transcripts_folder=os.getenv("TRANSCRIPTS_FOLDER", "").strip(),
            local_transcripts_dir=os.getenv("LOCAL_TRANSCRIPTS_DIR", "").strip(),
            email_overrides=_parse_email_overrides(os.getenv("EMAIL_OVERRIDES", "")),
            mail_domain=os.getenv("MAIL_DOMAIN", ""),
            email_enabled="SMTP_HOST" in os.environ,
            smtp_host=os.getenv("SMTP_HOST", "localhost"),
            smtp_port=int(os.getenv("SMTP_PORT", "25")),
            smtp_from=os.getenv("SMTP_FROM", ""),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_timeout=int(os.getenv("SMTP_TIMEOUT_SECONDS", "30")),
            smtp_tls_verify=os.getenv("SMTP_TLS_VERIFY", "auto"),
            retained_audio_max_age_days=int(
                os.getenv("RETAINED_AUDIO_MAX_AGE_DAYS", "30")
            ),
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "10")),
            notes_folder=os.getenv("NOTES_FOLDER", "/Talk/Notes"),
            audio_dir=os.getenv("AUDIO_DIR", "/tmp/notetaker-audio"),
            state_dir=os.getenv("STATE_DIR", "/data/notes"),
            heartbeat_file=os.getenv("HEARTBEAT_FILE", "/tmp/notetaker-heartbeat"),
        )
