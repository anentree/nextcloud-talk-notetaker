from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    nextcloud_url: str
    nextcloud_user: str
    nextcloud_password: str
    gemini_api_key: str
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
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            smtp_host=os.getenv("SMTP_HOST", "localhost"),
            smtp_port=int(os.getenv("SMTP_PORT", "25")),
            smtp_from=os.getenv("SMTP_FROM", ""),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "10")),
            notes_folder=os.getenv("NOTES_FOLDER", "/Talk/Notes"),
            audio_dir=os.getenv("AUDIO_DIR", "/tmp/notetaker-audio"),
        )
