"""NOTES_BACKEND=claude: notes written by Claude Code on a subscription.

The CLI tests run a real subprocess against a fake `claude` script, so the
argv, stdin, environment and JSON parsing are exercised end to end.
"""

import json
import os
import stat
from unittest.mock import patch

import pytest

from notetaker import claude_notes, retained
from notetaker.claude_notes import ClaudeError, ClaudeUnavailable, claude_call
from notetaker.config import Config
from notetaker.preflight import check_claude
from notetaker.reprocess import _load_sidecar
from notetaker.transcriber import (
    CLAUDE_WHISPER_MAX_FAILURES,
    WHISPER_MAX_FAILURES,
    make_notes,
)
from notetaker.whisper import WhisperFailed


def _fake_cli(tmp_path, body: str) -> str:
    """Write an executable stand-in for the claude binary and return its path."""
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(claude_notes, "RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(claude_notes, "POLL_SLICE", 0.05)


def test_prompt_goes_in_on_stdin_and_notes_come_back(tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    cli = _fake_cli(
        tmp_path,
        f"""
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ),
           "cwd": os.getcwd()}}, open({str(record)!r}, "w"))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "# Meeting Notes"}}))
""",
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-leak")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "must-not-leak")

    notes = claude_call("the transcript", model="haiku", token="tok")

    assert notes == "# Meeting Notes"
    seen = json.loads(record.read_text())
    assert seen["stdin"] == "the transcript"
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    # Untrusted transcript: no tools, no settings, no MCP servers.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "GEMINI_API_KEY" not in seen["env"]
    assert "NEXTCLOUD_PASSWORD" not in seen["env"]
    assert not os.path.exists(seen["cwd"]), "scratch directory should be removed"


def test_a_transient_error_is_retried(tmp_path, monkeypatch):
    counter = tmp_path / "count"
    cli = _fake_cli(
        tmp_path,
        f"""
p = {str(counter)!r}
n = int(open(p).read()) + 1 if os.path.exists(p) else 1
open(p, "w").write(str(n))
if n == 1:
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "API Error: 529 overloaded"}}))
else:
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": "# Notes"}}))
""",
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    assert claude_call("t", model="haiku", token="tok") == "# Notes"
    assert counter.read_text() == "2"


def test_a_rejected_token_fails_at_once(tmp_path, monkeypatch):
    counter = tmp_path / "count"
    cli = _fake_cli(
        tmp_path,
        f"""
open({str(counter)!r}, "a").write("x")
print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                  "result": "Failed to authenticate. API Error: 401 invalid OAuth token"}}))
""",
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        claude_call("t", model="haiku", token="bad")
    assert counter.read_text() == "x"


def test_persistent_outage_raises_unavailable(tmp_path, monkeypatch):
    cli = _fake_cli(
        tmp_path,
        """
print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                  "result": "API Error: 503 service unavailable"}))
""",
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeUnavailable):
        claude_call("t", model="haiku", token="tok")


def test_garbage_output_is_an_error_not_notes(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, 'print("not json"); sys.exit(1)\n')
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        claude_call("t", model="haiku", token="tok")


def test_missing_binary_is_a_config_error(monkeypatch):
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", "/nonexistent/claude")
    with pytest.raises(ClaudeError, match="not installed"):
        claude_call("t", model="haiku", token="tok")


def test_missing_token_fails_before_running_anything():
    with pytest.raises(ClaudeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        claude_call("t", model="haiku", token="")


def _counting_cli(tmp_path, payload: dict) -> tuple[str, object]:
    """A CLI that records each run and always answers with ``payload``."""
    counter = tmp_path / "count"
    cli = _fake_cli(
        tmp_path,
        f"""
open({str(counter)!r}, "a").write("x")
print(json.dumps({payload!r}))
sys.exit(1)
""",
    )
    return cli, counter


def test_status_401_is_an_auth_failure_whatever_the_wording(tmp_path, monkeypatch):
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "api_error_status": 401,
         "result": "Something unexpected"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        claude_call("t", model="haiku", token="tok")
    assert counter.read_text() == "x"


def test_status_404_is_not_retried(tmp_path, monkeypatch):
    # A bad model name: retrying cannot help, and it is not the token.
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "api_error_status": 404,
         "result": "There's an issue with the selected model; network rate limit"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        claude_call("t", model="nope", token="tok")
    assert counter.read_text() == "x"


def test_status_429_is_retried(tmp_path, monkeypatch):
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "api_error_status": 429,
         "result": "slow down"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeUnavailable):
        claude_call("t", model="haiku", token="tok")
    assert counter.read_text() == "x" * claude_notes.MAX_ATTEMPTS


def test_usage_limit_without_a_status_is_retried(tmp_path, monkeypatch):
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "result": "You've hit your limit · resets 5pm"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeUnavailable):
        claude_call("t", model="haiku", token="tok")
    assert counter.read_text() == "x" * claude_notes.MAX_ATTEMPTS


@pytest.mark.parametrize(
    "result",
    [
        # Exact CLI 2.1.281 output with an unresolvable API host.
        "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)",
        "API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)",
        "API Error: getaddrinfo EAI_AGAIN api.anthropic.com",
    ],
)
def test_network_failures_without_a_status_are_transient(tmp_path, monkeypatch, result):
    cli, counter = _counting_cli(
        tmp_path, {"subtype": "success", "is_error": True, "api_error_status": None, "result": result}
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeUnavailable):
        claude_call("t", model="haiku", token="tok")
    assert counter.read_text() == "x" * claude_notes.MAX_ATTEMPTS


def test_ordinary_words_are_not_mistaken_for_rate_limits(tmp_path, monkeypatch):
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "result": "could not generate an accurate answer"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        claude_call("t", model="haiku", token="tok")
    assert counter.read_text() == "x"


def test_cli_that_dies_before_reading_stdin_reports_its_own_error(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, 'sys.stderr.write("error: unknown option --tools\\n"); sys.exit(2)\n')
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    # Far bigger than a pipe buffer, like an hour-long transcript.
    with pytest.raises(ClaudeError, match="unknown option"):
        claude_call("x" * 2_000_000, model="haiku", token="tok")


# --- preflight ----------------------------------------------------------------


def test_preflight_starts_anyway_when_claude_is_only_unavailable():
    with patch(
        "notetaker.claude_notes.claude_call", side_effect=ClaudeUnavailable("usage limit")
    ):
        check_claude(_cfg())  # does not raise


def test_preflight_fails_on_a_bad_model_name(tmp_path, monkeypatch):
    cli, _ = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "api_error_status": 404,
         "result": "model: haiku-typo not found"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    with pytest.raises(ClaudeError):
        check_claude(_cfg(claude_model="haiku-typo"))


def test_preflight_makes_one_short_try(tmp_path, monkeypatch):
    cli, counter = _counting_cli(
        tmp_path,
        {"subtype": "success", "is_error": True, "api_error_status": 529, "result": "overloaded"},
    )
    monkeypatch.setattr(claude_notes, "CLAUDE_BIN", cli)

    check_claude(_cfg())  # overloaded at boot: start anyway

    assert counter.read_text() == "x"


def test_preflight_fails_on_a_rejected_token():
    with (
        patch("notetaker.claude_notes.claude_call", side_effect=ClaudeError("401")),
        pytest.raises(ClaudeError),
    ):
        check_claude(_cfg())


def test_preflight_passes_on_ok():
    with patch("notetaker.claude_notes.claude_call", return_value="OK"):
        check_claude(_cfg())


# --- wiring into make_notes -------------------------------------------------


def _cfg(**kw) -> Config:
    base = {
        "nextcloud_url": "https://nc.example.com",
        "nextcloud_user": "bot",
        "nextcloud_password": "pw",
        "nextcloud_web_password": "pw",
        "gemini_api_key": "",
        "transcription_backend": "whisper",
        "notes_backend": "claude",
        "claude_model": "haiku",
        "claude_oauth_token": "tok",
    }
    base.update(kw)
    return Config(**base)


SEGMENTS = [{"start": 0.0, "end": 4.0, "text": "Let's start"}]


@patch("notetaker.transcriber._get_audio_duration", return_value=3000.0)
@patch("notetaker.transcriber.summarize_transcript")
@patch("notetaker.transcriber.claude_call", return_value="# Claude notes")
@patch("notetaker.transcriber.whisper_transcribe", return_value=SEGMENTS)
def test_claude_backend_writes_the_notes_and_gemini_is_never_called(
    whisper_tr, claude, gemini_summary, _dur, tmp_path
):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {}

    notes = make_notes(
        _cfg(), meta, str(audio), "Standup", "2026-09-22", participant_names=["Jo"]
    )

    assert notes == "# Claude notes"
    gemini_summary.assert_not_called()
    prompt = claude.call_args.args[0]
    assert "Let's start" in prompt
    assert "Standup" in prompt and "2026-09-22" in prompt
    assert claude.call_args.kwargs["model"] == "haiku"
    assert claude.call_args.kwargs["token"] == "tok"
    assert meta["transcript"]


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe", side_effect=WhisperFailed("boom"))
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_claude_backend_never_falls_back_to_gemini_audio(gemini_audio, whisper_tr, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"whisper_failures": WHISPER_MAX_FAILURES}

    with pytest.raises(WhisperFailed):
        make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-22")

    gemini_audio.assert_not_called()
    whisper_tr.assert_called_once()


# --- config -----------------------------------------------------------------


def test_claude_backend_needs_no_gemini_key():
    assert _cfg().validate() == []


def test_claude_backend_needs_a_token_and_whisper():
    problems = " ".join(_cfg(claude_oauth_token="", transcription_backend="gemini").validate())
    assert "CLAUDE_CODE_OAUTH_TOKEN" in problems
    assert "TRANSCRIPTION_BACKEND=whisper" in problems


def test_gemini_backend_still_needs_its_key():
    problems = _cfg(notes_backend="gemini").validate()
    assert any("GEMINI_API_KEY" in p for p in problems)


def test_unknown_notes_backend_is_rejected():
    assert any("NOTES_BACKEND" in p for p in _cfg(notes_backend="openai").validate())


def test_env_loading(monkeypatch):
    for key, value in {
        "NEXTCLOUD_URL": "https://nc.example.com",
        "NEXTCLOUD_USER": "bot",
        "NEXTCLOUD_PASSWORD": "pw",
        "NOTES_BACKEND": "Claude",
        "CLAUDE_CODE_OAUTH_TOKEN": " tok ",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)

    cfg = Config.from_env()

    assert cfg.notes_backend == "claude"
    assert cfg.claude_oauth_token == "tok"
    assert cfg.claude_model == "haiku"
    assert cfg.gemini_api_key == ""


# --- parking a recording Whisper keeps failing on ---------------------------


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe")
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_claude_backend_parks_after_the_extra_whisper_tries(gemini_audio, whisper_tr, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"whisper_failures": CLAUDE_WHISPER_MAX_FAILURES}

    with pytest.raises(WhisperFailed, match="parked"):
        make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-22")

    assert meta["parked"] is True
    whisper_tr.assert_not_called()
    gemini_audio.assert_not_called()


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe")
def test_repeated_oom_kills_also_park(whisper_tr, _dur, tmp_path):
    # An OOM kill records no failure; only drain()'s pre-written attempts grow.
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"attempts": CLAUDE_WHISPER_MAX_FAILURES + 1}

    with pytest.raises(WhisperFailed, match="parked"):
        make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-22")
    whisper_tr.assert_not_called()


def test_drain_leaves_a_parked_recording_alone(tmp_path):
    src = tmp_path / "work" / "abc-2026-09-22-standup.webm"
    src.parent.mkdir()
    src.write_bytes(b"audio")
    meta = {"token": "abc", "name": "Standup", "call_start": "2026-09-22T10:00:00"}
    retained_dir = str(tmp_path / "retained")
    dest = retained.retain(retained_dir, str(src), meta, now=1000.0)

    def process(m, _audio):
        m["parked"] = True
        raise WhisperFailed("parked")

    retained.drain(retained_dir, process, now=1000.0 + 24 * 3600)
    with open(retained.sidecar_path(dest), encoding="utf-8") as f:
        assert json.load(f)["parked"] is True

    called = []
    retained.drain(retained_dir, lambda m, a: called.append(a), now=1000.0 + 90 * 24 * 3600)
    assert called == []


def test_reprocess_by_hand_unparks_and_starts_whisper_over(tmp_path):
    audio = tmp_path / "abc-2026-09-22-standup.webm"
    audio.write_bytes(b"audio")
    with open(retained.sidecar_path(str(audio)), "w", encoding="utf-8") as f:
        json.dump({"parked": True, "whisper_failures": 4, "attempts": 6, "speaker_events": []}, f)

    meta = _load_sidecar(str(audio))

    assert "parked" not in meta
    assert "whisper_failures" not in meta and "attempts" not in meta
    assert meta["speaker_events"] == []
