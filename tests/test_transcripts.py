"""The verbatim transcript: kept privately beside the notes, never emailed."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notetaker import retained, transcripts
from notetaker.config import Config

SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "Hello there."},
    {"start": 30.0, "end": 33.0, "text": "The budget is 4,200 dollars, not 4,000."},
]
TRANSCRIPT_LINES = [
    "[00:00] Hello there.",
    "[00:30] The budget is 4,200 dollars, not 4,000.",
]
NOTES = "# Notes\n\nSubject: Recap\n\nbody"
PARTICIPANTS = [
    {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"},
    {"user_id": "Jo", "display_name": "Jo", "email": "jo@y.com"},
]


def _cfg(tmp_path, **overrides):
    values = dict(  # noqa: C408
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        transcription_backend="whisper",
        notes_folder="/meeting-notes-summaries",
        transcripts_folder="/meeting-notes-transcriptions",
        state_dir=str(tmp_path / "state"),
        heartbeat_file=str(tmp_path / "hb"),
    )
    values.update(overrides)
    return Config(**values)


def _audio(tmp_path):
    audio = tmp_path / "work" / "2026-09-29-accounting-talk.webm"
    audio.parent.mkdir(exist_ok=True)
    audio.write_bytes(b"audio")
    return audio


def _recorder(audio, attendees=frozenset({"brantley", "Jo"})):
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], set(attendees)))
    return rec


def _run_call(cfg):
    from notetaker.main import handle_call

    asyncio.run(
        handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
    )


def _dest(cfg):
    return os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-29-accounting-talk.webm"
    )


# --- the document -------------------------------------------------------------


def test_document_keeps_every_line_verbatim_and_readable():
    doc = transcripts.build_document(
        "\n".join(TRANSCRIPT_LINES), "Accounting Talk", "2026-09-29 12:00", ["Jo"]
    )
    assert doc.startswith("# Transcript: Accounting Talk\n")
    assert "**Date:** 2026-09-29 12:00" in doc
    assert "**Attendees:** Jo" in doc
    # Exact text, and a blank line between utterances so Markdown does not
    # merge the whole call into one paragraph.
    assert "\n\n".join(TRANSCRIPT_LINES) in doc
    assert doc.endswith(TRANSCRIPT_LINES[-1] + "\n")


def test_document_omits_attendees_when_unknown():
    doc = transcripts.build_document("[00:00] hi", "Room", "2026-09-29 12:00", None)
    assert "Attendees" not in doc


# --- config -------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder",
    [
        "/meeting-notes",
        "/meeting-notes/",
        "meeting-notes",
        "/meeting-notes/transcripts",
        "/Meeting-Notes/transcripts",
        "/meeting-notes/../meeting-notes/x",
        "/",
    ],
)
def test_transcripts_folder_may_never_be_the_shared_notes_folder(tmp_path, folder):
    cfg = _cfg(tmp_path, notes_folder="/meeting-notes", transcripts_folder=folder)
    problems = cfg.validate()
    assert any("TRANSCRIPTS_FOLDER" in p for p in problems), problems


@pytest.mark.parametrize(
    "folder",
    ["/meeting-notes-transcriptions", "/private/transcripts", "meeting-notes2"],
)
def test_a_separate_transcripts_folder_is_accepted(tmp_path, folder):
    cfg = _cfg(tmp_path, notes_folder="/meeting-notes", transcripts_folder=folder)
    assert cfg.validate() == []


def test_local_transcripts_dir_must_differ_but_may_be_a_subfolder(tmp_path):
    same = _cfg(
        tmp_path,
        notes_storage="local",
        local_notes_dir="/data/notes",
        local_transcripts_dir="/data/notes/",
    )
    assert any("LOCAL_TRANSCRIPTS_DIR" in p for p in same.validate())
    sub = _cfg(
        tmp_path,
        notes_storage="local",
        local_notes_dir="/data/notes",
        local_transcripts_dir="/data/notes/transcripts",
    )
    assert sub.validate() == []


def test_transcripts_are_off_unless_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    assert Config.from_env().transcripts_enabled is False
    monkeypatch.setenv("TRANSCRIPTS_FOLDER", " /meeting-notes-transcriptions ")
    cfg = Config.from_env()
    assert cfg.transcripts_folder == "/meeting-notes-transcriptions"
    assert cfg.transcripts_enabled is True


# --- the pipeline -------------------------------------------------------------


@pytest.fixture
def whisper_pipeline():
    with (
        patch("notetaker.transcriber.whisper_transcribe", return_value=SEGMENTS) as w,
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
        patch("notetaker.main.get_participant_emails", return_value=PARTICIPANTS),
        patch("notetaker.main.upload_notes") as notes_upload,
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
        patch("notetaker.main.deliver_notes", return_value=True) as email,
    ):
        yield {
            "whisper": w,
            "notes_upload": notes_upload,
            "transcript_upload": transcript_upload,
            "email": email,
        }


def test_transcript_goes_to_its_own_folder_and_never_into_the_email(
    tmp_path, whisper_pipeline
):
    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    notes_call = whisper_pipeline["notes_upload"].call_args.args
    assert notes_call[3] == "/meeting-notes-summaries"
    assert notes_call[5] == NOTES

    whisper_pipeline["transcript_upload"].assert_called_once()
    folder, filename, document = whisper_pipeline["transcript_upload"].call_args.args[
        3:
    ]
    assert folder == "/meeting-notes-transcriptions"
    assert filename == notes_call[4]  # pairs with the summary by name
    for line in TRANSCRIPT_LINES:
        assert line in document
    assert "**Attendees:** Brantley Gunn, Jo" in document

    whisper_pipeline["email"].assert_called_once()
    envelope = whisper_pipeline["email"].call_args.args[1]
    flat = json.dumps(envelope)
    assert "4,200 dollars" not in flat
    assert "Hello there" not in flat
    assert envelope["notes"] == NOTES

    assert not audio.exists()
    assert not os.path.exists(_dest(cfg))


def test_no_transcript_is_written_when_the_folder_is_unset(tmp_path, whisper_pipeline):
    cfg = _cfg(tmp_path, transcripts_folder="")
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    whisper_pipeline["notes_upload"].assert_called_once()
    whisper_pipeline["transcript_upload"].assert_not_called()
    assert not audio.exists()


def test_a_failed_transcript_upload_keeps_the_recording_and_retries_only_that(
    tmp_path, whisper_pipeline
):
    """The notes went out; only the transcript upload failed. The recording is
    retained so the transcript is not lost, and the retry uploads just the
    transcript: no second Whisper run, no second notes upload, no second email."""
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    whisper_pipeline["transcript_upload"].side_effect = RuntimeError("Nextcloud 502")
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    dest = _dest(cfg)
    assert os.path.exists(dest)
    with open(retained.sidecar_path(dest), encoding="utf-8") as f:
        sidecar = json.load(f)
    assert sidecar["notes_saved"] is True
    assert sidecar["email_done"] is True
    assert "transcript_saved" not in sidecar
    assert sidecar["transcript"] == "\n".join(TRANSCRIPT_LINES)

    whisper_pipeline["transcript_upload"].reset_mock()
    whisper_pipeline["transcript_upload"].side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    whisper_pipeline["transcript_upload"].assert_called_once()
    assert whisper_pipeline["whisper"].call_count == 1
    assert whisper_pipeline["notes_upload"].call_count == 1
    assert whisper_pipeline["email"].call_count == 1
    assert not os.path.exists(dest)
    assert not os.path.exists(retained.sidecar_path(dest))


def test_a_failed_notes_upload_still_saves_the_transcript_once(
    tmp_path, whisper_pipeline
):
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    whisper_pipeline["notes_upload"].side_effect = RuntimeError("Nextcloud 502")
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    assert os.path.exists(_dest(cfg))
    whisper_pipeline["transcript_upload"].assert_called_once()

    whisper_pipeline["notes_upload"].side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    assert whisper_pipeline["notes_upload"].call_count == 2
    whisper_pipeline["transcript_upload"].assert_called_once()
    assert not os.path.exists(_dest(cfg))


def test_room_members_are_not_listed_as_attendees(tmp_path, whisper_pipeline):
    """With no observed attendees, recipients fall back to room membership —
    which says nothing about who was on the call."""
    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio, set())):
        _run_call(cfg)

    document = whisper_pipeline["transcript_upload"].call_args.args[5]
    assert "Attendees" not in document


def test_gemini_audio_path_has_no_transcript_and_is_not_held_up(tmp_path):
    cfg = _cfg(tmp_path, transcription_backend="gemini")
    audio = _audio(tmp_path)
    with (
        patch("notetaker.transcriber.transcribe_and_summarize", return_value=NOTES),
        patch("notetaker.main.get_participant_emails", return_value=PARTICIPANTS),
        patch("notetaker.main.upload_notes") as notes_upload,
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
        patch("notetaker.main.deliver_notes", return_value=True),
        patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)),
    ):
        _run_call(cfg)

    notes_upload.assert_called_once()
    transcript_upload.assert_not_called()
    assert not audio.exists()
    assert not os.path.exists(_dest(cfg))


def test_no_speech_writes_no_transcript(tmp_path, whisper_pipeline):
    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    whisper_pipeline["whisper"].return_value = []
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    whisper_pipeline["notes_upload"].assert_called_once()
    whisper_pipeline["transcript_upload"].assert_not_called()
    assert not audio.exists()


def test_local_storage_writes_the_transcript_to_its_own_directory(
    tmp_path, whisper_pipeline
):
    notes_dir = tmp_path / "notes"
    transcripts_dir = tmp_path / "notes" / "transcripts"
    cfg = _cfg(
        tmp_path,
        notes_storage="local",
        local_notes_dir=str(notes_dir),
        local_transcripts_dir=str(transcripts_dir),
    )
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    [notes_file] = [p for p in notes_dir.iterdir() if p.is_file()]
    [transcript_file] = list(transcripts_dir.iterdir())
    assert notes_file.name == transcript_file.name
    assert notes_file.read_text() == NOTES
    assert TRANSCRIPT_LINES[1] in transcript_file.read_text()
    whisper_pipeline["transcript_upload"].assert_not_called()


# --- reprocess.py ---------------------------------------------------------------


def _reprocess_env(monkeypatch, transcripts_folder="/meeting-notes-transcriptions"):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("NOTES_FOLDER", "/meeting-notes-summaries")
    if transcripts_folder:
        monkeypatch.setenv("TRANSCRIPTS_FOLDER", transcripts_folder)


def _retained_with_transcript(tmp_path):
    audio = tmp_path / "itujzrjw-2026-09-29-accounting-talk.webm"
    audio.write_bytes(b"x")
    with open(retained.sidecar_path(str(audio)), "w", encoding="utf-8") as f:
        json.dump({"transcript": "\n".join(TRANSCRIPT_LINES)}, f)
    return audio


def test_reprocess_uploads_the_transcript_privately(tmp_path, monkeypatch):
    from notetaker import reprocess

    _reprocess_env(monkeypatch)
    audio = _retained_with_transcript(tmp_path)
    with (
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
        patch("notetaker.reprocess.upload_notes") as notes_upload,
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
    ):
        assert reprocess.main([str(audio)]) == 0

    assert notes_upload.call_args.args[3] == "/meeting-notes-summaries"
    assert transcript_upload.call_args.args[3] == "/meeting-notes-transcriptions"
    assert transcript_upload.call_args.args[4] == notes_upload.call_args.args[4]
    assert TRANSCRIPT_LINES[1] in transcript_upload.call_args.args[5]


def test_reprocess_skips_the_transcript_when_not_configured(tmp_path, monkeypatch):
    from notetaker import reprocess

    _reprocess_env(monkeypatch, transcripts_folder="")
    audio = _retained_with_transcript(tmp_path)
    with (
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
        patch("notetaker.reprocess.upload_notes") as notes_upload,
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
    ):
        assert reprocess.main([str(audio)]) == 0

    notes_upload.assert_called_once()
    transcript_upload.assert_not_called()


def test_reprocess_out_writes_the_transcript_beside_the_notes(tmp_path, monkeypatch):
    from notetaker import reprocess

    _reprocess_env(monkeypatch)
    audio = _retained_with_transcript(tmp_path)
    out = tmp_path / "recovered.md"
    with (
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
    ):
        assert reprocess.main([str(audio), "--out", str(out)]) == 0

    assert out.read_text() == NOTES
    assert TRANSCRIPT_LINES[0] in (tmp_path / "recovered-transcript.md").read_text()
