from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

from notetaker.mailer import TLS_VERIFY_AUTO, TLS_VERIFY_FALSE, TLS_VERIFY_TRUE

_TLS_VERIFY_ACCEPTED = TLS_VERIFY_TRUE | TLS_VERIFY_FALSE | {TLS_VERIFY_AUTO}


def _normalise(value: str | None) -> str:
    return (value or "").strip().lower()


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
    filename_last_user: str = ""
    notes_storage: str = "nextcloud"
    local_notes_dir: str = ""
    email_overrides: dict[str, str] = field(default_factory=dict)
    mail_domain: str = ""
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
        if not 1 <= self.smtp_port <= 65535:
            problems.append(f"SMTP_PORT {self.smtp_port} is not a valid port")
        if bool(self.smtp_user) != bool(self.smtp_password):
            problems.append(
                "SMTP_USER and SMTP_PASSWORD must be set together (or both left blank)"
            )
        if self.smtp_from and "@" not in self.smtp_from:
            problems.append(f"SMTP_FROM is not an email address: {self.smtp_from!r}")
        if self.notes_storage not in ("nextcloud", "local"):
            problems.append(
                f"NOTES_STORAGE must be 'nextcloud' or 'local' (got {self.notes_storage!r})"
            )
        if self.notes_storage == "local" and not self.local_notes_dir:
            problems.append("NOTES_STORAGE=local requires LOCAL_NOTES_DIR")
        if self.auth_method not in ("nextcloud", "yunohost"):
            problems.append(
                f"AUTH_METHOD must be 'nextcloud' or 'yunohost' (got {self.auth_method!r})"
            )
        if self.poll_interval < 1:
            problems.append("POLL_INTERVAL_SECONDS must be >= 1")
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
            "GEMINI_API_KEY",
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
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            auth_method=os.getenv("AUTH_METHOD", "nextcloud"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            filename_last_user=os.getenv("FILENAME_LAST_USER", ""),
            notes_storage=os.getenv("NOTES_STORAGE", "nextcloud"),
            local_notes_dir=os.getenv("LOCAL_NOTES_DIR", ""),
            email_overrides=_parse_email_overrides(os.getenv("EMAIL_OVERRIDES", "")),
            mail_domain=os.getenv("MAIL_DOMAIN", ""),
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
