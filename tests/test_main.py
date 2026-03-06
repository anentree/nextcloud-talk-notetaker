import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from notetaker.main import handle_call
from notetaker.config import Config


@patch("notetaker.main.send_notes_email")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_handle_call_pipeline(
    mock_recorder_cls, mock_transcribe, mock_participants, mock_upload, mock_email
):
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(return_value="/tmp/audio.wav")
    mock_recorder_cls.return_value = mock_recorder

    mock_transcribe.return_value = "# Meeting: Standup\n\n## Summary\nGood stuff."
    mock_participants.return_value = [
        {"user_id": "alice", "display_name": "Alice", "email": "alice@example.com"},
    ]

    cfg = Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        gemini_api_key="gemini-key",
    )
    room = {"token": "abc123", "displayName": "Standup"}

    asyncio.run(handle_call(cfg, room))

    mock_recorder.record_call.assert_called_once_with("abc123", "Standup")
    mock_transcribe.assert_called_once()
    mock_upload.assert_called_once()
    mock_email.assert_called_once()
