import pytest
from notetaker.config import Config


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    cfg = Config.from_env()

    assert cfg.nextcloud_url == "https://nc.example.com"
    assert cfg.nextcloud_user == "bot"
    assert cfg.nextcloud_password == "secret"
    assert cfg.gemini_api_key == "gemini-key"
    assert cfg.poll_interval == 10  # default
    assert cfg.notes_folder == "/Talk/Notes"  # default


def test_config_raises_on_missing_required(monkeypatch):
    monkeypatch.delenv("NEXTCLOUD_URL", raising=False)
    monkeypatch.delenv("NEXTCLOUD_USER", raising=False)
    monkeypatch.delenv("NEXTCLOUD_PASSWORD", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="NEXTCLOUD_URL"):
        Config.from_env()
