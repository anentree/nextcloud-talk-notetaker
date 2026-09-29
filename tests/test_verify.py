"""Content verification: is this call's output real enough to delete the audio?"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notetaker import retained
from notetaker.config import Config
from notetaker.transcriber import NO_SPEECH_SENTINEL
from notetaker.verify import check_content, speech_chars

NOTES = "# Meeting Notes: Accounting Talk\n\n" + "Discussed the budget. " * 20


def _cfg(**overrides):
    values = {
        "nextcloud_url": "https://nc.example.com",
        "nextcloud_user": "bot",
        "nextcloud_password": "s",
        "nextcloud_web_password": "s",
        "gemini_api_key": "k",
    }
    values.update(overrides)
    return Config(**values)


def _speech(chars_per_minute, minutes):
    """A transcript with the given density, in real [MM:SS] line format."""
    lines, total, i = [], chars_per_minute * minutes, 0
    while total > 0:
        # Distinct lines: repeated ones are counted once (hallucinations).
        words = f"line {i} " + "word " * 20
        lines.append(f"[{i // 60:02d}:{i % 60:02d}] Neo: {words.strip()}")
        total -= len(words.strip())
        i += 10
    return "\n".join(lines)


def test_speech_chars_ignores_timestamps():
    assert speech_chars("[00:00] hello\n[01:05] Neo: yes") == len("hello" + "Neo: yes")


def test_a_repeated_hallucination_counts_once():
    """Whisper on silence: the same phrase every 30 seconds for an hour."""
    lines = [
        f"[{m:02d}:{s:02d}] Thanks for watching!" for m in range(78) for s in (0, 30)
    ]
    assert speech_chars("\n".join(lines)) == len("Thanks for watching!")
    problems = check_content(_cfg(), NOTES, "\n".join(lines), 78 * 60, "x")
    assert any("speech" in p for p in problems)


def test_gemini_markers_are_not_speech():
    transcript = (
        "[NO SPEECH DETECTED]\n[SEGMENT 2 OF 6 UNAVAILABLE — covers minutes 15-30]"
    )
    assert speech_chars(transcript) == 0


def test_the_2026_09_29_call_fails():
    """78 minutes, 24 characters of speech — the call that started this."""
    problems = check_content(
        _cfg(), NOTES, "[00:12] Thanks for watching!", 78 * 60, NO_SPEECH_SENTINEL
    )
    assert len(problems) == 1
    assert "speech" in problems[0] and "78-minute" in problems[0]


@pytest.mark.parametrize(
    "per_minute,minutes",
    # Every real call on the server 2026-09-22..29, by characters per minute.
    [(453, 107), (790, 17), (430, 69), (75, 184), (67, 23), (690, 33)],
)
def test_real_calls_from_the_last_week_pass(per_minute, minutes):
    transcript = _speech(per_minute, minutes)
    assert check_content(_cfg(), NOTES, transcript, minutes * 60, "x") == []


def test_no_speech_on_a_long_call_fails():
    problems = check_content(
        _cfg(), NOTES, NO_SPEECH_SENTINEL, 40 * 60, NO_SPEECH_SENTINEL
    )
    assert problems and problems[0].startswith("only 0 characters of speech")


def test_short_calls_are_not_judged_on_speech():
    assert (
        check_content(_cfg(), NOTES, NO_SPEECH_SENTINEL, 4 * 60, NO_SPEECH_SENTINEL)
        == []
    )


def test_empty_notes_fail_even_with_good_speech():
    problems = check_content(_cfg(), "# Notes\n", _speech(500, 30), 30 * 60, "x")
    assert problems == ["the notes are empty (7 characters)"]


def test_no_transcript_on_a_long_call_fails_closed():
    """Gemini single-pass writes notes straight from audio: nothing to check
    them against, so they are not trusted."""
    problems = check_content(_cfg(), NOTES, None, 60 * 60, "x")
    assert problems and "no transcript" in problems[0]


def test_no_transcript_on_a_short_call_is_judged_on_notes_only():
    assert check_content(_cfg(), NOTES, None, 4 * 60, "x") == []
    assert check_content(_cfg(), "", None, 4 * 60, "x")


def test_short_calls_with_sparse_speech_pass():
    assert check_content(_cfg(), NOTES, "[00:03] ok bye", 4 * 60, "x") == []


def test_unknown_duration_fails_closed():
    assert check_content(_cfg(), NOTES, _speech(500, 30), None, "x") == [
        "the length of the recording could not be read"
    ]


def test_thresholds_are_configurable():
    strict = _cfg(min_speech_chars_per_minute=500, verify_min_minutes=1)
    assert check_content(strict, NOTES, _speech(300, 10), 10 * 60, "x")
    lax = _cfg(min_speech_chars_per_minute=0)
    assert check_content(lax, NOTES, "[00:00] hi", 78 * 60, "x") == []


def test_a_short_silent_call_with_its_placeholder_notes_passes():
    from notetaker.transcriber import _no_speech_notes

    notes = _no_speech_notes("Accounting Talk", "2026-09-29", 2)
    assert (
        check_content(_cfg(), notes, NO_SPEECH_SENTINEL, 120, NO_SPEECH_SENTINEL) == []
    )


def test_a_long_silent_call_with_placeholder_notes_fails():
    from notetaker.transcriber import _no_speech_notes

    notes = _no_speech_notes("Accounting Talk", "2026-09-29", 78)
    problems = check_content(
        _cfg(), notes, NO_SPEECH_SENTINEL, 78 * 60, NO_SPEECH_SENTINEL
    )
    assert any("speech" in p for p in problems)


# --- the pipeline: hold instead of publish ------------------------------------


PARTICIPANTS = [
    {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"},
    {"user_id": "piyush", "display_name": "Piyush", "email": "p@kmk.com"},
]
SILENT = [{"start": 12.0, "end": 14.0, "text": "Thanks for watching!"}]
TALKING = [
    {"start": float(i * 10), "end": float(i * 10 + 9), "text": "word " * 40}
    for i in range(400)
]


def _pipeline_cfg(tmp_path, **overrides):
    values = {
        "transcription_backend": "whisper",
        "notes_folder": "/meeting-notes-summaries",
        "transcripts_folder": "/meeting-notes-transcriptions",
        "alert_email": "neo@x.com",
        "state_dir": str(tmp_path / "state"),
        "heartbeat_file": str(tmp_path / "hb"),
    }
    values.update(overrides)
    return _cfg(**values)


def _run_call(cfg, tmp_path, segments, minutes=78, notes=NOTES):
    from notetaker.main import handle_call

    audio = tmp_path / "work" / "2026-09-29-accounting-talk.webm"
    audio.parent.mkdir(exist_ok=True)
    audio.write_bytes(b"audio")
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], {"brantley", "piyush"}))
    mocks = {}
    with (
        patch("notetaker.transcriber.whisper_transcribe", return_value=segments),
        patch("notetaker.transcriber._get_audio_duration", return_value=minutes * 60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=notes),
        patch("notetaker.main.get_participant_emails", return_value=PARTICIPANTS),
        patch("notetaker.main.upload_notes") as mocks["notes"],
        patch("notetaker.transcripts.upload_notes") as mocks["transcript"],
        patch("notetaker.main.deliver_notes", return_value=True) as mocks["email"],
        patch("notetaker.main.AudioRecorder", return_value=rec),
    ):
        asyncio.run(
            handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
        )
    held = os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-29-accounting-talk.webm"
    )
    return audio, held, mocks


def test_a_silent_call_is_held_not_published_and_neo_is_told(tmp_path):
    cfg = _pipeline_cfg(tmp_path)
    audio, held, mocks = _run_call(cfg, tmp_path, SILENT)

    mocks["notes"].assert_not_called()  # nothing in the shared folder
    [alert] = [c.args[1] for c in mocks["email"].call_args_list]
    assert [r["email"] for r in alert["recipients"]] == ["neo@x.com"]
    assert "speech" in alert["body"] and "--release" in alert["body"]
    assert "p@kmk.com" not in json.dumps(alert)

    assert not audio.exists()
    assert os.path.exists(held)  # the recording is kept
    with open(retained.sidecar_path(held), encoding="utf-8") as f:
        sidecar = json.load(f)
    assert sidecar["parked"] is True and sidecar["held"]
    assert sidecar["alert_sent"] is True
    # The transcript still goes to the private folder, for review.
    assert mocks["transcript"].call_args.args[3] == "/meeting-notes-transcriptions"


def test_a_real_call_is_published_and_the_recording_deleted(tmp_path):
    cfg = _pipeline_cfg(tmp_path)
    audio, held, mocks = _run_call(cfg, tmp_path, TALKING, minutes=60)

    mocks["notes"].assert_called_once()
    [email] = [c.args[1] for c in mocks["email"].call_args_list]
    assert [r["email"] for r in email["recipients"]] == ["b@x.com", "p@kmk.com"]
    assert not audio.exists() and not os.path.exists(held)


def test_without_an_alert_address_the_call_is_still_held(tmp_path):
    cfg = _pipeline_cfg(tmp_path, alert_email="")
    _, held, mocks = _run_call(cfg, tmp_path, SILENT)
    mocks["notes"].assert_not_called()
    mocks["email"].assert_not_called()
    assert os.path.exists(held)


def test_empty_notes_are_held_even_on_the_gemini_path(tmp_path):
    from notetaker.main import handle_call

    cfg = _pipeline_cfg(tmp_path, transcription_backend="gemini")
    audio = tmp_path / "work" / "2026-09-29-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], {"brantley"}))
    with (
        patch("notetaker.transcriber.transcribe_and_summarize", return_value="# Notes"),
        patch("notetaker.transcriber._get_audio_duration", return_value=3600.0),
        patch("notetaker.main.get_participant_emails", return_value=PARTICIPANTS),
        patch("notetaker.main.upload_notes") as notes_upload,
        patch("notetaker.main.deliver_notes", return_value=True) as email,
        patch("notetaker.main.AudioRecorder", return_value=rec),
    ):
        asyncio.run(handle_call(cfg, {"token": "itujzrjw", "displayName": "Talk"}))
    notes_upload.assert_not_called()
    assert [r["email"] for r in email.call_args.args[1]["recipients"]] == ["neo@x.com"]


def test_a_held_recording_is_never_retried_on_its_own(tmp_path):
    from notetaker.main import drain_retained

    cfg = _pipeline_cfg(tmp_path)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    with (
        patch("notetaker.retained.time.time", return_value=9e9),
        patch("notetaker.main._retry_recording") as retry,
    ):
        drain_retained(cfg)
    retry.assert_not_called()
    assert os.path.exists(held)


def _release_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "s")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("SMTP_HOST", "host.docker.internal")
    monkeypatch.setenv("NOTES_FOLDER", "/meeting-notes-summaries")
    monkeypatch.setenv("TRANSCRIPTS_FOLDER", "/meeting-notes-transcriptions")


def test_release_delivers_a_held_call_once_and_deletes_it(tmp_path, monkeypatch):
    from notetaker import reprocess

    cfg = _pipeline_cfg(tmp_path)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    _release_env(monkeypatch)
    monkeypatch.setenv("STATE_DIR", cfg.state_dir)
    with (
        patch("notetaker.main.upload_notes") as notes_upload,
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
        patch("notetaker.main.deliver_notes", return_value=True) as email,
        patch("notetaker.transcriber.summarize_transcript") as summarize,
        patch("notetaker.transcriber.whisper_transcribe") as whisper,
    ):
        assert reprocess.main([held, "--release"]) == 0

    notes_upload.assert_called_once()
    assert notes_upload.call_args.args[5] == NOTES  # the notes it already made
    summarize.assert_not_called()
    whisper.assert_not_called()
    transcript_upload.assert_not_called()  # saved privately during the hold
    [envelope] = [c.args[1] for c in email.call_args_list]
    assert [r["email"] for r in envelope["recipients"]] == ["b@x.com", "p@kmk.com"]
    assert not os.path.exists(held)
    assert not os.path.exists(retained.sidecar_path(held))


def test_release_refuses_a_recording_that_was_not_held(tmp_path, monkeypatch):
    from notetaker import reprocess

    _release_env(monkeypatch)
    audio = tmp_path / "itujzrjw-2026-09-29-talk.webm"
    audio.write_bytes(b"x")
    with open(retained.sidecar_path(str(audio)), "w", encoding="utf-8") as f:
        json.dump({"token": "itujzrjw", "parked": True}, f)
    with patch("notetaker.main._retry_recording") as retry:
        assert reprocess.main([str(audio), "--release"]) == 1
    retry.assert_not_called()
    assert audio.exists()


def test_a_failed_release_keeps_the_recording_and_what_was_done(tmp_path, monkeypatch):
    from notetaker import reprocess

    cfg = _pipeline_cfg(tmp_path)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    _release_env(monkeypatch)
    monkeypatch.setenv("STATE_DIR", cfg.state_dir)
    with (
        patch("notetaker.main.upload_notes", side_effect=RuntimeError("502")),
        patch("notetaker.main.deliver_notes", return_value=True) as email,
    ):
        assert reprocess.main([held, "--release"]) == 1
    assert os.path.exists(held)
    with open(retained.sidecar_path(held), encoding="utf-8") as f:
        sidecar = json.load(f)
    assert sidecar["email_done"] is True  # a second release must not re-send
    assert email.call_count == 1


def test_the_alert_names_the_exact_held_file(tmp_path):
    cfg = _pipeline_cfg(tmp_path)
    _, held, mocks = _run_call(cfg, tmp_path, SILENT)
    body = mocks["email"].call_args.args[1]["body"]
    assert f"--release /data/notes/retained-audio/{os.path.basename(held)}" in body


def test_held_recordings_are_never_pruned(tmp_path):
    from notetaker.main import prune_retained_audio

    cfg = _pipeline_cfg(tmp_path, retained_audio_max_age_days=30)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    ordinary = os.path.join(cfg.retained_audio_dir, "abc-2026-08-01-x.webm")
    with open(ordinary, "wb") as f:
        f.write(b"a")
    old = 1_000_000_000
    for path in (held, retained.sidecar_path(held), ordinary):
        os.utime(path, (old, old))
    prune_retained_audio(cfg)
    assert os.path.exists(held) and os.path.exists(retained.sidecar_path(held))
    assert not os.path.exists(ordinary)


def test_an_unreadable_length_falls_back_to_time_in_the_call(tmp_path):
    from notetaker.main import _verify_content

    meta = {"transcript": "[00:00] Thanks for watching!", "recorded_s": 78 * 60.0}
    with patch(
        "notetaker.transcriber._get_audio_duration", side_effect=RuntimeError("x")
    ):
        problems = _verify_content(_cfg(), meta, "/nope.webm", NOTES)
    assert meta["duration_s"] == 78 * 60.0
    assert any("speech" in p for p in problems)


def test_an_already_published_recording_is_not_held_on_retry(tmp_path):
    from notetaker.main import _verify_content

    for done in ({"notes_saved": True}, {"email_done": True}):
        meta = {"transcript": "[00:00] hi", "duration_s": 3600.0, **done}
        assert _verify_content(_cfg(), meta, "/nope.webm", NOTES) == []


def test_gemini_segment_transcripts_are_checked(tmp_path):
    """The chunked Gemini path hands its segment transcripts to the check."""
    from notetaker.transcriber import make_notes

    cfg = _cfg(transcription_backend="gemini")
    meta = {}

    def fake(*args, transcript_sink=None, **kwargs):
        transcript_sink.extend(["[NO SPEECH DETECTED]", "[NO SPEECH DETECTED]"])
        return NOTES

    with patch("notetaker.transcriber.transcribe_and_summarize", side_effect=fake):
        make_notes(cfg, meta, "/a.webm", "Talk", "2026-09-29")
    assert meta["gemini_transcript"] == "[NO SPEECH DETECTED]\n[NO SPEECH DETECTED]"

    from notetaker.main import _verify_content

    meta["duration_s"] = 60 * 60.0
    assert _verify_content(cfg, meta, "/a.webm", NOTES)


def test_plain_reprocess_refuses_to_publish_a_held_call(tmp_path, monkeypatch):
    from notetaker import reprocess

    cfg = _pipeline_cfg(tmp_path)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    _release_env(monkeypatch)
    with (
        patch("notetaker.reprocess.upload_notes") as upload,
        patch("notetaker.reprocess.make_notes") as make,
    ):
        assert reprocess.main([held]) == 1
    upload.assert_not_called()
    make.assert_not_called()
    assert os.path.exists(held)


def test_an_empty_recording_with_attendees_alerts(tmp_path):
    from notetaker.main import handle_call

    cfg = _pipeline_cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-29-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"")
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], {"piyush"}))
    with (
        patch("notetaker.main.deliver_notes", return_value=True) as email,
        patch("notetaker.main.AudioRecorder", return_value=rec),
    ):
        asyncio.run(handle_call(cfg, {"token": "itujzrjw", "displayName": "Talk"}))
    [envelope] = [c.args[1] for c in email.call_args_list]
    assert [r["email"] for r in envelope["recipients"]] == ["neo@x.com"]
    assert "no audio" in envelope["subject"].lower()


def test_a_failed_release_is_left_for_the_automatic_retry(tmp_path, monkeypatch):
    from notetaker import reprocess

    cfg = _pipeline_cfg(tmp_path)
    _, held, _ = _run_call(cfg, tmp_path, SILENT)
    _release_env(monkeypatch)
    monkeypatch.setenv("STATE_DIR", cfg.state_dir)
    with (
        patch("notetaker.main.upload_notes", side_effect=RuntimeError("502")),
        patch("notetaker.main.deliver_notes", return_value=True),
    ):
        assert reprocess.main([held, "--release"]) == 1
    with open(retained.sidecar_path(held), encoding="utf-8") as f:
        sidecar = json.load(f)
    assert "parked" not in sidecar and sidecar["content_accepted"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_speech_chars_per_minute": -1},
        {"verify_min_minutes": -1},
        {"alert_email": "neo"},
    ],
)
def test_bad_verification_settings_are_refused(overrides):
    assert _cfg(**overrides).validate()


def test_the_silence_alert_goes_to_the_alert_address_only(tmp_path):
    from notetaker.main import _alert_silence

    cfg = _pipeline_cfg(tmp_path, smtp_host="host.docker.internal")
    with patch("notetaker.main.deliver_notes", return_value=True) as email:
        _alert_silence(cfg, "itujzrjw", "Accounting Talk", 420.0, 2, 1)
    envelope = email.call_args.args[1]
    assert [r["email"] for r in envelope["recipients"]] == ["neo@x.com"]
    assert "7 minutes" in envelope["body"] and "NOT recording" in envelope["body"]
