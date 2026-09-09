"""Shared test fixtures.

Config.from_env() calls load_dotenv(), which reads the developer's real
.env from the working directory. That made config tests pass or fail
depending on whose machine they ran on — the deployment .env sets
NOTES_FOLDER=/meeting-notes, so the "default value" assertions failed.
Neutralise it for the whole suite and start every test from a clean
environment.
"""

from __future__ import annotations

import pytest

NOTETAKER_ENV_VARS = (
    "NEXTCLOUD_URL",
    "NEXTCLOUD_USER",
    "NEXTCLOUD_PASSWORD",
    "NEXTCLOUD_WEB_PASSWORD",
    "AUTH_METHOD",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "FILENAME_LAST_USER",
    "NOTES_STORAGE",
    "NOTES_FOLDER",
    "LOCAL_NOTES_DIR",
    "EMAIL_OVERRIDES",
    "MAIL_DOMAIN",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_FROM",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "AUDIO_DIR",
    "STATE_DIR",
    "HEARTBEAT_FILE",
    "VAD_THRESHOLD",
    "SKIP_PREFLIGHT",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Stop the real .env from leaking into tests."""
    monkeypatch.setattr("notetaker.config.load_dotenv", lambda *a, **k: False)
    for var in NOTETAKER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
