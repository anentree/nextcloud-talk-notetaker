from datetime import date
from unittest.mock import MagicMock, patch
from notetaker.transcriber import (
    transcribe_and_summarize,
    SEGMENT_PROMPT,
    SYNTHESIS_PROMPT,
)


def test_segment_prompt_contains_required_fields():
    assert "{seg_num}" in SEGMENT_PROMPT
    assert "{total_segs}" in SEGMENT_PROMPT
    assert "{conversation_name}" in SEGMENT_PROMPT


def test_synthesis_prompt_contains_required_sections():
    assert "Topics Discussed" in SYNTHESIS_PROMPT
    assert "Action Items" in SYNTHESIS_PROMPT
    assert "Decisions Made" in SYNTHESIS_PROMPT
    assert "Follow-Up Email" in SYNTHESIS_PROMPT


def _prompt_of(mock_client) -> str:
    """The text part of the last generate_content call."""
    contents = mock_client.models.generate_content.call_args.kwargs["contents"]
    return next(c for c in contents if isinstance(c, str))


def _ffmpeg_side_effect(args, **kwargs):
    """Side effect that creates the output MP3 file when ffmpeg is called."""
    # Find the output path (last arg that ends with .mp3)
    for arg in reversed(args):
        if isinstance(arg, str) and arg.endswith(".mp3"):
            with open(arg, "wb") as f:
                f.write(b"fake mp3 data")
            break
    return MagicMock(returncode=0, stdout="", stderr="")


@patch("notetaker.transcriber._get_audio_duration", return_value=300.0)
@patch("notetaker.transcriber.genai")
@patch("notetaker.transcriber.subprocess")
def test_transcribe_and_summarize_short(mock_sub, mock_genai, mock_duration, tmp_path):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client

    expected_notes = "# Meeting: Standup\n\n## Summary\nWe discussed things."
    mock_response = MagicMock()
    mock_response.text = expected_notes
    mock_client.models.generate_content.return_value = mock_response

    audio_file = tmp_path / "test.webm"
    audio_file.write_bytes(b"fake audio data")

    mock_sub.run.side_effect = _ffmpeg_side_effect

    result = transcribe_and_summarize("fake-api-key", str(audio_file), "Daily Standup")

    assert result == expected_notes
    mock_client.models.generate_content.assert_called_once()
    call_args = mock_client.models.generate_content.call_args
    assert call_args.kwargs["model"] == "gemini-2.5-flash-lite"


@patch("notetaker.transcriber._get_audio_duration", return_value=300.0)
@patch("notetaker.transcriber.genai")
@patch("notetaker.transcriber.subprocess")
def test_custom_model_parameter(mock_sub, mock_genai, mock_duration, tmp_path):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "notes"
    mock_client.models.generate_content.return_value = mock_response

    audio_file = tmp_path / "test.webm"
    audio_file.write_bytes(b"fake audio data")

    mock_sub.run.side_effect = _ffmpeg_side_effect

    transcribe_and_summarize(
        "fake-key", str(audio_file), "Sprint Planning", model="gemini-2.0-flash"
    )

    call_args = mock_client.models.generate_content.call_args
    assert call_args.kwargs["model"] == "gemini-2.0-flash"


@patch("notetaker.transcriber._get_audio_duration", return_value=300.0)
@patch("notetaker.transcriber.genai")
@patch("notetaker.transcriber.subprocess")
def test_call_date_is_used_instead_of_today(
    mock_sub, mock_genai, mock_duration, tmp_path
):
    """A retained recording is retried hours or days after the call. The
    notes header, the email subject and the "use this date" instruction
    used date.today(), so a Monday call retried on Tuesday was dated
    Tuesday."""
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "notes"
    mock_client.models.generate_content.return_value = mock_response
    audio_file = tmp_path / "test.webm"
    audio_file.write_bytes(b"fake audio data")
    mock_sub.run.side_effect = _ffmpeg_side_effect

    transcribe_and_summarize(
        "fake-key", str(audio_file), "Sprint Planning", call_date="2026-09-08"
    )

    prompt = _prompt_of(mock_client)
    assert "2026-09-08" in prompt
    assert date.today().isoformat() not in prompt  # noqa: DTZ011 - mirrors the source


@patch("notetaker.transcriber._get_audio_duration", return_value=300.0)
@patch("notetaker.transcriber.genai")
@patch("notetaker.transcriber.subprocess")
def test_call_date_defaults_to_today(mock_sub, mock_genai, mock_duration, tmp_path):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "notes"
    mock_client.models.generate_content.return_value = mock_response
    audio_file = tmp_path / "test.webm"
    audio_file.write_bytes(b"fake audio data")
    mock_sub.run.side_effect = _ffmpeg_side_effect

    transcribe_and_summarize("fake-key", str(audio_file), "Sprint Planning")

    prompt = _prompt_of(mock_client)
    assert date.today().isoformat() in prompt  # noqa: DTZ011 - mirrors the source
