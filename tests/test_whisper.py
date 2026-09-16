"""Local transcription must be quota-free, heartbeat-friendly and quarantined.

faster_whisper is a native dependency the Gemini-only install never uses; it
must not be able to break that install's startup, and no test here may load
a real model or touch the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from notetaker import whisper


class FakeSegment(SimpleNamespace):
    pass


class FakeModel:
    """Stands in for faster_whisper.WhisperModel."""

    instances: list = []

    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.transcribe_calls: list = []
        FakeModel.instances.append(self)

    def transcribe(self, path, **kwargs):
        self.transcribe_calls.append((path, kwargs))
        segments = iter(
            [
                FakeSegment(start=0.0, end=2.0, text=" Hello there "),
                FakeSegment(start=2.0, end=3.0, text="   "),
                FakeSegment(start=3.5, end=5.0, text="Bye"),
            ]
        )
        return segments, SimpleNamespace(duration=5.0, language="en")


@pytest.fixture
def fake_model(monkeypatch):
    FakeModel.instances = []
    monkeypatch.setattr(whisper, "_model_factory", FakeModel)
    return FakeModel


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        wav = args[-1]
        with open(wav, "wb") as f:
            f.write(b"RIFF")
        return MagicMock(returncode=0)

    monkeypatch.setattr(whisper.subprocess, "run", run)
    return calls


def test_transcribe_converts_with_ffmpeg_then_returns_clean_segments(tmp_path, fake_model, fake_ffmpeg):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")

    out = whisper.transcribe(str(audio), "large-v3-turbo", "/models", language="en")

    assert out == [
        {"start": 0.0, "end": 2.0, "text": "Hello there"},
        {"start": 3.5, "end": 5.0, "text": "Bye"},
    ]
    args, kwargs = fake_ffmpeg[0]
    assert args[:3] == ["ffmpeg", "-y", "-i"]
    assert args[3] == str(audio)
    for flag in ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"):
        assert flag in args
    assert kwargs.get("check") is True
    wav_path = args[-1]
    assert wav_path.endswith(".wav") and not wav_path.startswith(str(tmp_path))
    assert not os.path.exists(wav_path), "the temporary WAV must be removed"

    model = fake_model.instances[0]
    assert model.name == "large-v3-turbo"
    assert model.kwargs["device"] == "cpu"
    assert model.kwargs["compute_type"] == "int8"
    assert model.kwargs["download_root"] == "/models"
    assert model.kwargs["local_files_only"] is True
    assert model.kwargs["cpu_threads"] >= 1
    path, tkwargs = model.transcribe_calls[0]
    assert path == wav_path
    assert tkwargs["vad_filter"] is True
    assert tkwargs["language"] == "en"
    assert tkwargs["initial_prompt"] == whisper.INITIAL_PROMPT


def test_empty_language_means_auto_detect(tmp_path, fake_model, fake_ffmpeg):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    whisper.transcribe(str(audio), "m", "/models", language="")
    assert fake_model.instances[0].transcribe_calls[0][1]["language"] is None


def test_heartbeat_fires_around_every_slow_step(tmp_path, fake_model, fake_ffmpeg):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    beats = []
    whisper.transcribe(str(audio), "m", "/models", heartbeat_fn=lambda: beats.append(1))
    # after ffmpeg, after model load, before transcribe, then one per segment (3)
    assert len(beats) >= 3 + 3


def test_a_raising_heartbeat_is_not_a_whisper_failure(tmp_path, fake_model, fake_ffmpeg):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")

    def bad_beat():
        raise OSError("disk full")

    out = whisper.transcribe(str(audio), "m", "/models", heartbeat_fn=bad_beat)
    assert len(out) == 2


def test_ffmpeg_failure_becomes_whisper_failed_and_cleans_up(tmp_path, fake_model, monkeypatch):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    created = []

    def run(args, **kwargs):
        created.append(args[-1])
        raise subprocess.CalledProcessError(1, args, stderr=b"Invalid data found")

    monkeypatch.setattr(whisper.subprocess, "run", run)
    with pytest.raises(whisper.WhisperFailed) as excinfo:
        whisper.transcribe(str(audio), "m", "/models")
    assert "Invalid data found" in str(excinfo.value)
    assert not os.path.exists(created[0])
    assert fake_model.instances == []


def test_model_errors_become_whisper_failed(tmp_path, fake_ffmpeg, monkeypatch):
    class Exploding:
        def __init__(self, *a, **k):
            raise MemoryError("cannot allocate")

    monkeypatch.setattr(whisper, "_model_factory", Exploding)
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    with pytest.raises(whisper.WhisperFailed) as excinfo:
        whisper.transcribe(str(audio), "m", "/models")
    assert isinstance(excinfo.value.__cause__, MemoryError)
    wav = fake_ffmpeg[0][0][-1]
    assert not os.path.exists(wav), "the temporary WAV must be removed on failure too"


def test_download_uses_the_configured_name_and_directory(monkeypatch):
    calls = []

    def fake_download(name, **kwargs):
        calls.append((name, kwargs))
        return "/models/snapshot"

    monkeypatch.setattr(whisper, "_download_fn", fake_download)
    assert whisper.download("medium", "/models") == "/models/snapshot"
    assert calls == [("medium", {"cache_dir": "/models"})]


def test_model_is_downloaded_true_false_and_unknown_name(monkeypatch):
    def fake_download(name, **kwargs):
        assert kwargs["local_files_only"] is True
        if name == "present":
            return "/models/snapshot"
        if name == "absent":
            raise FileNotFoundError("no snapshot")
        raise ValueError(f"Invalid model size '{name}'")

    monkeypatch.setattr(whisper, "_download_fn", fake_download)
    assert whisper.model_is_downloaded("present", "/models") is True
    assert whisper.model_is_downloaded("absent", "/models") is False
    with pytest.raises(ValueError):
        whisper.model_is_downloaded("typo", "/models")


def test_download_from_env_needs_no_other_settings_and_reports_progress(monkeypatch, capsys, tmp_path):
    for var in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_PASSWORD", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.setenv("WHISPER_MODEL_DIR", str(tmp_path))
    calls = []

    def slow_download(name, **kw):
        (tmp_path / "blob").write_bytes(b"x" * (3 * 1024 * 1024))
        time.sleep(0.15)
        calls.append((name, kw))
        return "/p"

    monkeypatch.setattr(whisper, "_download_fn", slow_download)

    assert whisper.download_from_env(progress_every=0.05) == 0
    assert calls == [("small", {"cache_dir": str(tmp_path)})]
    out = capsys.readouterr().out
    assert "small" in out
    assert "3 MB on disk so far" in out


def test_download_from_env_reports_failure(monkeypatch, capsys):
    def boom(name, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr(whisper, "_download_fn", boom)
    assert whisper.download_from_env() == 1
    assert "no route to host" in capsys.readouterr().err


def test_main_dispatches_download_model_before_reading_config(monkeypatch):
    from notetaker import main as main_mod

    monkeypatch.setattr(sys, "argv", ["notetaker", "download-model"])
    monkeypatch.setattr(whisper, "download_from_env", lambda: 7)
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code == 7


def test_modules_import_without_faster_whisper(monkeypatch):
    """A Gemini-only install must never fail to start because a native
    library of a backend it does not use will not load."""
    code = (
        "import sys; sys.modules['faster_whisper'] = None; "
        "import notetaker.whisper, notetaker.transcriber, notetaker.main; "
        "print('imports-ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "imports-ok" in result.stdout
    # And the lazy resolver is what raises, with a normal ImportError.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(ImportError):
        whisper._resolve_model_factory()
