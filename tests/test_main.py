import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from notetaker.main import handle_call, _build_notes_filename
from notetaker.config import Config
from datetime import datetime
from tests.notes_fixture import NOTES_BODY


@patch("notetaker.main.os.unlink")
@patch("notetaker.main.os.path.getsize", return_value=1024)
@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.transcriber.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_handle_call_pipeline(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    mock_getsize,
    mock_unlink,
):
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(
        return_value=("/tmp/audio.wav", [], {"alice"})
    )
    mock_recorder_cls.return_value = mock_recorder

    mock_transcribe.return_value = (
        "# Meeting: Standup\n\n## Summary\nGood stuff." + NOTES_BODY
    )
    mock_participants.return_value = [
        {"user_id": "alice", "display_name": "Alice", "email": "alice@example.com"},
    ]

    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
    )
    room = {"token": "abc123", "displayName": "Standup"}

    asyncio.run(handle_call(cfg, room))

    mock_recorder.record_call.assert_called_once_with("abc123", "Standup")
    mock_transcribe.assert_called_once()
    mock_upload.assert_called_once()
    mock_email.assert_called_once()
    mock_unlink.assert_called_once_with("/tmp/audio.wav")


@patch("notetaker.main.os.unlink")
@patch("notetaker.main.os.path.getsize", return_value=0)
@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.transcriber.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_handle_call_skips_empty_audio(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    mock_getsize,
    mock_unlink,
):
    """Empty audio file should skip transcription and email."""
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(
        return_value=("/tmp/audio.wav", [], {"alice"})
    )
    mock_recorder_cls.return_value = mock_recorder

    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
    )
    room = {"token": "abc123", "displayName": "Standup"}

    asyncio.run(handle_call(cfg, room))

    mock_recorder.record_call.assert_called_once()
    mock_transcribe.assert_not_called()
    mock_upload.assert_not_called()
    mock_email.assert_not_called()


def test_build_notes_filename_alphabetical():
    participants = [
        {"user_id": "charlie", "display_name": "Charlie Brown"},
        {"user_id": "alice", "display_name": "Alice Smith"},
    ]
    now = datetime(2026, 3, 11, 9, 15)
    result = _build_notes_filename(participants, now)
    assert result == "2026-03-11-0915-alice-charlie.md"


def test_build_notes_filename_with_last_user():
    participants = [
        {"user_id": "charlie", "display_name": "Charlie Brown"},
        {"user_id": "alice", "display_name": "Alice Smith"},
        {"user_id": "boss", "display_name": "Boss Man"},
    ]
    now = datetime(2026, 3, 11, 9, 15)
    result = _build_notes_filename(participants, now, last_user="boss")
    assert result == "2026-03-11-0915-alice-charlie-boss.md"


def test_build_notes_filename_no_last_user():
    participants = [
        {"user_id": "bob", "display_name": "Bob Jones"},
    ]
    now = datetime(2026, 3, 11, 14, 30)
    result = _build_notes_filename(participants, now, last_user="")
    assert result == "2026-03-11-1430-bob.md"


@patch("notetaker.main.os.unlink")
@patch("notetaker.main.os.path.getsize", return_value=1024)
@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.transcriber.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_handle_call_does_not_email_when_email_is_disabled(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    mock_getsize,
    mock_unlink,
):
    """No SMTP_HOST means no mail server to talk to. The notes are still
    stored; the email step is skipped rather than attempted and spooled."""
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(
        return_value=("/tmp/audio.wav", [], {"alice"})
    )
    mock_recorder_cls.return_value = mock_recorder
    mock_transcribe.return_value = (
        "# Meeting: Standup\n\n## Summary\nGood stuff." + NOTES_BODY
    )
    mock_participants.return_value = [
        {"user_id": "alice", "display_name": "Alice", "email": "alice@example.com"},
    ]
    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        email_enabled=False,
    )

    asyncio.run(handle_call(cfg, {"token": "abc123", "displayName": "Standup"}))

    mock_upload.assert_called_once()
    mock_email.assert_not_called()
    mock_unlink.assert_called_once_with("/tmp/audio.wav")


def test_finish_recording_dates_the_notes_by_the_call_not_the_retry(tmp_path):
    """A retained recording is retried hours or days later; the notes must
    carry the date the call happened."""
    from notetaker.main import _finish_recording

    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        notes_storage="local",
        local_notes_dir=str(tmp_path / "notes"),
        heartbeat_file=str(tmp_path / "hb"),
    )
    meta = {
        "token": "t",
        "name": "Call",
        "call_start": "2026-09-08T16:01:39",
        "speaker_events": [],
        "recorded_s": 120.0,
    }
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x")
    with (
        patch(
            "notetaker.transcriber.transcribe_and_summarize",
            return_value="# N" + NOTES_BODY,
        ) as tr,
        patch("notetaker.main.deliver_notes", return_value=True),
    ):
        assert _finish_recording(cfg, meta, str(audio), []) is True
    assert tr.call_args.kwargs["call_date"] == "2026-09-08"
