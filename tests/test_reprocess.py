"""Tests for the retained-recording replay tool."""

import json
from unittest.mock import patch

from notetaker import reprocess
from notetaker.reprocess import parse_retained_name


def test_parses_token_date_and_name_from_retained_filename():
    token, call_date, name = parse_retained_name(
        "/data/notes/retained-audio/92gwpi2v-2026-08-25-brantley-gunn-jo-ai-notetaker.webm"
    )
    assert token == "92gwpi2v"
    assert call_date == "2026-08-25"
    assert name == "Brantley Gunn Jo Ai Notetaker"


def test_unparseable_name_still_yields_a_usable_conversation_name():
    token, call_date, name = parse_retained_name("/tmp/some-recording.webm")
    assert token == ""
    assert call_date == ""
    assert name == "Some Recording"


def test_reprocess_passes_the_date_from_the_filename(monkeypatch, tmp_path):
    """The retained name carries the call date; the notes must use it, not
    the day someone got round to reprocessing."""
    from unittest.mock import patch

    from notetaker import reprocess

    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    audio = tmp_path / "92gwpi2v-2026-08-25-brantley-gunn-jo.webm"
    audio.write_bytes(b"x")

    with patch("notetaker.transcriber.transcribe_and_summarize", return_value="n") as tr:
        assert reprocess.main([str(audio), "--dry-run"]) == 0
    assert tr.call_args.kwargs["call_date"] == "2026-08-25"

    undated = tmp_path / "some-recording.webm"
    undated.write_bytes(b"x")
    with patch("notetaker.transcriber.transcribe_and_summarize", return_value="n") as tr:
        assert reprocess.main([str(undated), "--dry-run"]) == 0
    assert tr.call_args.kwargs["call_date"] is None


def _set_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")


def test_reprocess_uses_the_sidecar_timeline_when_present(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    audio = tmp_path / "itujzrjw-2026-08-25-accounting-talk.webm"
    audio.write_bytes(b"x")
    (tmp_path / "itujzrjw-2026-08-25-accounting-talk.webm.json").write_text(
        json.dumps({"speaker_events": [{"label": "Jo", "start_ms": 0, "end_ms": 1000}]})
    )
    with patch("notetaker.transcriber.transcribe_and_summarize", return_value="n") as tr:
        assert reprocess.main([str(audio), "--dry-run"]) == 0
    assert tr.call_args.kwargs["speaker_events"] == [{"label": "Jo", "start_ms": 0, "end_ms": 1000}]


def test_reprocess_without_a_sidecar_sends_no_timeline(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    audio = tmp_path / "itujzrjw-2026-08-25-accounting-talk.webm"
    audio.write_bytes(b"x")
    with patch("notetaker.transcriber.transcribe_and_summarize", return_value="n") as tr:
        assert reprocess.main([str(audio), "--dry-run"]) == 0
    assert tr.call_args.kwargs["speaker_events"] == []
