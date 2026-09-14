"""Regression tests for the 2026-08-25 loss of a 79-minute meeting.

A real call was recorded, chunked into six 15-minute segments, and three of
them transcribed successfully (14887, 17529 and 19350 chars). Segment 4
returned HTTP 200 with no text part, `response.text` was None, and
`len(text)` raised TypeError — discarding all three good segments along with
the bad one.

Two separate defects are covered here:
  1. _gemini_call must never hand a None back to its callers. The SDK types
     `GenerateContentResponse.text` as Optional[str], so None is part of the
     contract on a *successful* call, and no exception is raised for the
     retry loop to catch.
  2. One unusable segment must not destroy the segments that did work.
"""

from unittest.mock import MagicMock, patch

import pytest

from notetaker.transcriber import (
    EmptyGeminiResponse,
    _gemini_call,
    transcribe_and_summarize,
)


def _response(text, finish_reason="STOP"):
    """A generate_content return value whose .text is `text`."""
    resp = MagicMock()
    resp.text = text
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    resp.candidates = [candidate]
    return resp


def test_gemini_call_never_returns_none():
    """HTTP 200 with no text part must not surface as None to the caller."""
    client = MagicMock()
    client.models.generate_content.return_value = _response(None, "MAX_TOKENS")

    # The empty response is treated as transient, so this walks the whole
    # retry budget - which is minutes long since 2026-09-14. Never sleep it.
    with (
        patch("notetaker.transcriber.time.sleep"),
        pytest.raises(Exception) as exc_info,
    ):
        _gemini_call(client, ["prompt"])

    # It must fail loudly rather than return None, and say why.
    assert "MAX_TOKENS" in str(exc_info.value)
    assert not isinstance(exc_info.value, TypeError), (
        "should raise a described error, not crash on len(None) downstream"
    )


def test_gemini_call_retries_empty_then_succeeds():
    """An empty response gets exactly one retry: it is usually deterministic
    (MAX_TOKENS, SAFETY, RECITATION on the same input), but one retry is
    cheap insurance against the odd flake."""
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _response(None, "MAX_TOKENS"),
        _response("recovered text"),
    ]

    with patch("notetaker.transcriber.time.sleep"):
        result = _gemini_call(client, ["prompt"])

    assert result == "recovered text"
    assert client.models.generate_content.call_count == 2


def test_persistently_empty_response_gives_up_after_two_calls():
    """Each attempt re-uploads the whole segment. Walking the full 8-attempt,
    395-second transient budget for a response that will be empty every
    time is pure cost - and delays the notes for the rest of the meeting."""
    client = MagicMock()
    client.models.generate_content.return_value = _response(None, "SAFETY")

    with (
        patch("notetaker.transcriber.time.sleep") as sleep,
        pytest.raises(EmptyGeminiResponse),
    ):
        _gemini_call(client, ["prompt"])

    assert client.models.generate_content.call_count == 2
    assert sum(c.args[0] for c in sleep.call_args_list) < 60


def _ffmpeg_side_effect(args, **kwargs):
    for arg in reversed(args):
        if isinstance(arg, str) and arg.endswith(".mp3"):
            with open(arg, "wb") as f:
                f.write(b"fake mp3 data")
            break
    return MagicMock(returncode=0, stdout="", stderr="")


@patch("notetaker.transcriber._get_audio_duration", return_value=4752.0)
@patch("notetaker.transcriber.genai")
@patch("notetaker.transcriber.subprocess")
def test_one_dead_segment_does_not_discard_the_others(
    mock_sub, mock_genai, mock_duration, tmp_path
):
    """Reproduces 2026-08-25: segment 4 of 6 dies, notes must still be produced."""
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_sub.run.side_effect = _ffmpeg_side_effect

    good = "Speaker said something substantial. " * 600  # passes the density gate

    def responses():
        # segments 1-3 fine, segment 4 empty on both of its attempts, the
        # third empty is segment 5's first try (it recovers on its retry),
        # segment 6 fine, then the synthesis call.
        yield _response(good)
        yield _response(good)
        yield _response(good)
        yield _response(None, "MAX_TOKENS")
        yield _response(None, "MAX_TOKENS")
        yield _response(None, "MAX_TOKENS")
        while True:
            yield _response(good)

    mock_client.models.generate_content.side_effect = responses()

    audio_file = tmp_path / "call.webm"
    audio_file.write_bytes(b"fake audio data")

    with patch("notetaker.transcriber.time.sleep"):
        result = transcribe_and_summarize(
            "fake-api-key", str(audio_file), "Brantley Gunn, Jo, AI Notetaker"
        )

    assert result, "a dead segment must not wipe out the whole meeting"
    assert isinstance(result, str)


def test_notes_disclose_which_minutes_are_missing():
    """A failed segment must be visible in the notes, not silently dropped.

    The synthesis prompt tells the model to use only what the transcripts
    contain, so it omits the placeholder a failed segment leaves behind. The
    disclosure therefore has to be added in code.
    """
    from notetaker.transcriber import _prepend_coverage_warning

    notes = "# Meeting Notes\n\nEverything looked fine."
    out = _prepend_coverage_warning(notes, [(1, 0.0, 900.0, "503 UNAVAILABLE")], 6)

    assert "Incomplete transcript" in out
    assert "1 of 6 segments" in out
    assert "0–15" in out
    assert "503 UNAVAILABLE" in out
    assert notes in out, "the notes themselves must survive intact"


def test_no_warning_when_every_segment_transcribed():
    from notetaker.transcriber import _prepend_coverage_warning

    notes = "# Meeting Notes\n\nAll good."
    assert _prepend_coverage_warning(notes, [], 6) == notes
