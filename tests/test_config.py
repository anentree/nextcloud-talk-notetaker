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


def _required_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")


def test_email_is_enabled_when_smtp_host_is_set(monkeypatch):
    """Every .env the wizard writes with email on carries SMTP_HOST, as does
    the deployed server's; that presence is the whole on/off switch."""
    _required_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "host.docker.internal")
    assert Config.from_env().email_enabled is True


def test_email_is_disabled_when_smtp_host_is_unset(monkeypatch):
    """Declining email in the wizard writes the SMTP keys commented out.
    Before 2026-09-14 the code then fell back to localhost:25, preflight
    tried to connect to it inside the container, and the container
    crash-looped from the wizard's own default answer."""
    _required_env(monkeypatch)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    cfg = Config.from_env()
    assert cfg.email_enabled is False
    assert cfg.validate() == [], "unset SMTP values must not be config problems"


def test_direct_construction_keeps_email_on():
    """Config(...) built by hand (tests, tools) behaves as before."""
    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
    )
    assert cfg.email_enabled is True


def test_transcription_settings_default_to_gemini(monkeypatch):
    for k, v in {
        "NEXTCLOUD_URL": "https://nc.example.com",
        "NEXTCLOUD_USER": "bot",
        "NEXTCLOUD_PASSWORD": "pw",
        "GEMINI_API_KEY": "key",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert cfg.transcription_backend == "gemini"
    assert cfg.whisper_model == "large-v3-turbo"
    assert cfg.whisper_model_dir == "/data/models"
    assert cfg.whisper_language == "en"
    assert cfg.validate() == []


def test_transcription_settings_are_read_and_normalised(monkeypatch):
    for k, v in {
        "NEXTCLOUD_URL": "https://nc.example.com",
        "NEXTCLOUD_USER": "bot",
        "NEXTCLOUD_PASSWORD": "pw",
        "GEMINI_API_KEY": "key",
        "TRANSCRIPTION_BACKEND": " Whisper ",
        "WHISPER_MODEL": "medium",
        "WHISPER_MODEL_DIR": "/models",
        "WHISPER_LANGUAGE": "",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert cfg.transcription_backend == "whisper"
    assert cfg.whisper_model == "medium"
    assert cfg.whisper_model_dir == "/models"
    assert cfg.whisper_language == ""


def test_unknown_transcription_backend_is_a_config_problem():
    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="pw",
        nextcloud_web_password="pw",
        gemini_api_key="key",
        transcription_backend="deepgram",
    )
    problems = cfg.validate()
    assert any("TRANSCRIPTION_BACKEND" in p and "deepgram" in p for p in problems)
