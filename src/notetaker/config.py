from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


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
    poll_interval: int = 10
    notes_folder: str = "/Talk/Notes"
    audio_dir: str = "/tmp/notetaker-audio"

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
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "10")),
            notes_folder=os.getenv("NOTES_FOLDER", "/Talk/Notes"),
            audio_dir=os.getenv("AUDIO_DIR", "/tmp/notetaker-audio"),
        )
