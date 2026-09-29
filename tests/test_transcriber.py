from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from notetaker.transcriber import (
    UpstreamUnavailable,
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


from notetaker.config import Config
from notetaker.transcriber import (
    NO_SPEECH_SENTINEL,
    TRANSCRIPT_SYNTHESIS_PROMPT,
    WHISPER_MAX_FAILURES,
    make_notes,
    summarize_transcript,
)
from notetaker.whisper import WhisperFailed


def _cfg(**kw) -> Config:
    base = dict(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="pw",
        nextcloud_web_password="pw",
        gemini_api_key="key",
        transcription_backend="whisper",
        whisper_model="large-v3-turbo",
        whisper_model_dir="/data/models",
        whisper_language="en",
    )
    base.update(kw)
    return Config(**base)


SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Let's start"},
    {"start": 10.0, "end": 14.0, "text": "I agree"},
]
EVENTS = [
    {"label": "Jo", "start_ms": 0, "end_ms": 5000},
    {"label": "Brantley", "start_ms": 9000, "end_ms": 15000},
]


def test_transcript_prompt_keeps_the_notes_format_and_takes_a_transcript():
    for section in ("Topics Discussed", "Action Items", "Decisions Made", "Follow-Up Email"):
        assert section in TRANSCRIPT_SYNTHESIS_PROMPT
    assert "{transcript}" in TRANSCRIPT_SYNTHESIS_PROMPT
    assert "{participants_block}" in TRANSCRIPT_SYNTHESIS_PROMPT
    assert "{timeline_block}" not in TRANSCRIPT_SYNTHESIS_PROMPT


@patch("notetaker.transcriber.genai")
def test_summarize_transcript_makes_one_text_only_call(mock_genai):
    client = mock_genai.Client.return_value
    client.models.generate_content.return_value = MagicMock(text="# Notes")

    notes = summarize_transcript(
        "key", "[00:00] Jo: hi", "Standup", "2026-09-15", 66, participant_names=["Jo"]
    )

    assert notes == "# Notes"
    client.models.generate_content.assert_called_once()
    contents = client.models.generate_content.call_args.kwargs["contents"]
    assert len(contents) == 1 and isinstance(contents[0], str)
    assert "[00:00] Jo: hi" in contents[0]
    assert "Standup" in contents[0] and "2026-09-15" in contents[0] and "66-minute" in contents[0]
    assert "POSSIBLE PARTICIPANTS" in contents[0]


@patch("notetaker.transcriber._get_audio_duration", return_value=3960.0)
@patch("notetaker.transcriber.summarize_transcript", return_value="# Notes")
@patch("notetaker.transcriber.whisper_transcribe", return_value=SEGMENTS)
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_whisper_backend_transcribes_labels_and_summarizes_once(
    gemini_audio, whisper_tr, summarize, _dur, tmp_path
):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"speaker_events": EVENTS}
    beats = []

    notes = make_notes(
        _cfg(), meta, str(audio), "Standup", "2026-09-15",
        participant_names=["Jo", "Brantley"], heartbeat_fn=lambda: beats.append(1),
    )

    assert notes == "# Notes"
    gemini_audio.assert_not_called()
    whisper_tr.assert_called_once()
    wkw = whisper_tr.call_args.kwargs
    assert whisper_tr.call_args.args[0] == str(audio)
    assert wkw["model_name"] == "large-v3-turbo"
    assert wkw["model_dir"] == "/data/models"
    assert wkw["language"] == "en"
    assert wkw["heartbeat_fn"] is not None
    skw = summarize.call_args.kwargs
    assert summarize.call_args.args[1] == "[00:00] Jo: Let's start\n[00:10] Brantley: I agree"
    assert skw["duration_min"] == 66
    assert skw["participant_names"] == ["Jo", "Brantley"]
    assert skw["model"] == "gemini-2.5-flash-lite"
    assert meta["transcript"] == "[00:00] Jo: Let's start\n[00:10] Brantley: I agree"


@patch("notetaker.transcriber._get_audio_duration", return_value=3960.0)
@patch("notetaker.transcriber.summarize_transcript", return_value="# Notes")
@patch("notetaker.transcriber.whisper_transcribe", return_value=SEGMENTS)
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_malformed_timeline_keeps_the_transcript_unlabelled(
    gemini_audio, whisper_tr, summarize, _dur, tmp_path
):
    """A hand-edited sidecar with a malformed speaker_events entry must not
    cost the whole 45-minute transcription on retry — keep the transcript,
    just without speaker labels.

    Note: `label_transcript` raises KeyError on the missing `start_ms` key
    (verified directly against src/notetaker/attribution.py), so this is
    the exception path, not the tolerant one. The brief's assertion text
    said the fallback transcript would contain "Unattributed"; verified
    that `label_transcript(segments, [])` produces plain unlabelled lines
    (`by_label` is `{}`, which is falsy, so `_label_for` — the only place
    that emits the literal "Unattributed" — is never called). Asserting on
    the unlabelled text instead, which is what the fallback actually does.
    """
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"speaker_events": [{"label": "Jo"}]}  # missing start_ms/end_ms

    notes = make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")

    assert notes == "# Notes"
    assert meta["transcript"] == "[00:00] Let's start\n[00:10] I agree"
    assert "Let's start" in meta["transcript"]
    gemini_audio.assert_not_called()


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.summarize_transcript", return_value="# Notes")
@patch("notetaker.transcriber.whisper_transcribe")
def test_cached_transcript_skips_whisper(whisper_tr, summarize, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"transcript": "[00:00] Jo: cached"}

    make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")

    whisper_tr.assert_not_called()
    assert summarize.call_args.args[1] == "[00:00] Jo: cached"


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.summarize_transcript")
@patch("notetaker.transcriber.whisper_transcribe", return_value=[])
def test_no_speech_is_terminal_and_costs_no_gemini_call(whisper_tr, summarize, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {}

    notes = make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")

    assert meta["transcript"] == NO_SPEECH_SENTINEL
    summarize.assert_not_called()
    assert "No speech was detected" in notes and "Standup" in notes and "2026-09-15" in notes

    # A retry with the sentinel cached must not run Whisper again.
    whisper_tr.reset_mock()
    make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")
    whisper_tr.assert_not_called()


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe", side_effect=WhisperFailed("boom"))
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_whisper_failure_is_counted_and_propagates(gemini_audio, whisper_tr, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {}
    with pytest.raises(WhisperFailed):
        make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")
    assert meta["whisper_failures"] == 1
    assert "transcript" not in meta
    gemini_audio.assert_not_called()


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe")
@patch("notetaker.transcriber.transcribe_and_summarize", return_value="# Gemini notes")
def test_fallback_to_gemini_audio_after_repeated_whisper_failures(gemini_audio, whisper_tr, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"whisper_failures": WHISPER_MAX_FAILURES, "speaker_events": EVENTS}

    notes = make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15", participant_names=["Jo"])

    assert notes == "# Gemini notes"
    whisper_tr.assert_not_called()
    kw = gemini_audio.call_args.kwargs
    assert gemini_audio.call_args.args == ("key", str(audio), "Standup")
    assert kw["speaker_events"] == EVENTS
    assert kw["participant_names"] == ["Jo"]
    assert kw["call_date"] == "2026-09-15"
    assert kw["model"] == "gemini-2.5-flash-lite"


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.whisper_transcribe")
@patch("notetaker.transcriber.transcribe_and_summarize", return_value="# Gemini notes")
def test_fallback_when_attempts_died_without_a_recorded_failure(gemini_audio, whisper_tr, _dur, tmp_path):
    """An OOM-killed attempt leaves no whisper_failures behind; drain() now
    commits `attempts` before running, so that count is the second guard."""
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"attempts": WHISPER_MAX_FAILURES + 1}
    make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")
    whisper_tr.assert_not_called()
    gemini_audio.assert_called_once()


@patch("notetaker.transcriber._get_audio_duration", return_value=600.0)
@patch("notetaker.transcriber.summarize_transcript", side_effect=UpstreamUnavailable("503"))
@patch("notetaker.transcriber.whisper_transcribe", return_value=SEGMENTS)
def test_gemini_outage_after_whisper_keeps_the_transcript(whisper_tr, summarize, _dur, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"speaker_events": EVENTS}
    with pytest.raises(UpstreamUnavailable):
        make_notes(_cfg(), meta, str(audio), "Standup", "2026-09-15")
    assert meta["transcript"].startswith("[00:00] Jo:")
    assert meta.get("whisper_failures", 0) == 0


@patch("notetaker.transcriber.whisper_transcribe")
@patch("notetaker.transcriber.transcribe_and_summarize", return_value="# Gemini notes")
def test_gemini_backend_forwards_everything_as_keywords(gemini_audio, whisper_tr, tmp_path):
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"speaker_events": EVENTS}
    beat = lambda: None  # noqa: E731

    notes = make_notes(
        _cfg(transcription_backend="gemini", gemini_model="gemini-2.5-flash"),
        meta, str(audio), "Standup", None, participant_names=["Jo"], heartbeat_fn=beat,
    )

    assert notes == "# Gemini notes"
    whisper_tr.assert_not_called()
    assert gemini_audio.call_args.args == ("key", str(audio), "Standup")
    kw = dict(gemini_audio.call_args.kwargs)
    assert kw.pop("transcript_sink") == []
    assert kw == {
        "model": "gemini-2.5-flash",
        "speaker_events": EVENTS,
        "participant_names": ["Jo"],
        "heartbeat_fn": beat,
        "call_date": None,
    }


@patch("notetaker.transcriber._get_audio_duration", return_value=3960.0)
@patch("notetaker.transcriber.summarize_transcript", return_value="# Notes")
@patch("notetaker.transcriber.whisper_transcribe")
@patch("notetaker.transcriber.transcribe_and_summarize")
def test_cached_transcript_is_summarized_even_on_the_gemini_backend(
    gemini_audio, whisper_tr, summarize, _dur, tmp_path
):
    """Switching whisper -> gemini while a retained recording already holds
    a finished Whisper transcript must not re-upload the audio."""
    audio = tmp_path / "call.webm"
    audio.write_bytes(b"x")
    meta = {"transcript": "[00:00] Jo: Let's start"}

    notes = make_notes(
        _cfg(transcription_backend="gemini"), meta, str(audio), "Standup", "2026-09-15"
    )

    assert notes == "# Notes"
    gemini_audio.assert_not_called()
    whisper_tr.assert_not_called()
    summarize.assert_called_once()
    assert summarize.call_args.args[1] == "[00:00] Jo: Let's start"
