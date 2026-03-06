from unittest.mock import MagicMock, patch
from notetaker.transcriber import transcribe_and_summarize, PROMPT_TEMPLATE


def test_prompt_template_contains_required_sections():
    assert "Summary" in PROMPT_TEMPLATE
    assert "Key Takeaways" in PROMPT_TEMPLATE
    assert "Action Items" in PROMPT_TEMPLATE
    assert "Decisions Made" in PROMPT_TEMPLATE
    assert "Follow-Up Email" in PROMPT_TEMPLATE


@patch("notetaker.transcriber.genai")
def test_transcribe_and_summarize(mock_genai, tmp_path):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client

    expected_notes = "# Meeting: Standup\n\n## Summary\nWe discussed things."
    mock_response = MagicMock()
    mock_response.text = expected_notes
    mock_client.models.generate_content.return_value = mock_response

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"fake audio data")

    result = transcribe_and_summarize("fake-api-key", str(audio_file), "Daily Standup")

    assert result == expected_notes
    mock_client.models.generate_content.assert_called_once()
    call_args = mock_client.models.generate_content.call_args
    assert call_args.kwargs["model"] == "gemini-2.5-flash"


@patch("notetaker.transcriber.genai")
def test_prompt_includes_conversation_name(mock_genai, tmp_path):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "notes"
    mock_client.models.generate_content.return_value = mock_response

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"fake audio data")

    transcribe_and_summarize("fake-key", str(audio_file), "Sprint Planning")

    call_args = mock_client.models.generate_content.call_args
    contents = call_args.kwargs["contents"]
    prompt_text = [c for c in contents if isinstance(c, str)][0]
    assert "Sprint Planning" in prompt_text
