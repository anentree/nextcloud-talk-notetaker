"""Regression tests for the 2026-09-08 loss of a 44-minute meeting.

Gemini answered 503 "This model is currently experiencing high demand" on and
off for about two and a half minutes (16:46:47 to past 16:49:04). The retry
loop in _gemini_call gave each call three attempts spaced 5s and 10s apart —
a 15-second window against a multi-minute outage. Segment 1 lost, segments 2
and 3 survived by luck, and the final synthesis call hit three 503s in 19s
and raised; the meeting had no notes and the recording sat in retained-audio
with nothing ever retrying it.

Two defects:
  1. The retry window must be shaped like the outage it exists for: minutes,
     not seconds — and the container heartbeat must stay fresh while waiting,
     or the healthcheck declares the container dead for doing its job.
  2. A recording retained after the pipeline fails must be retried on its
     own later (tests/test_retained.py), not wait for a human.
"""

from unittest.mock import MagicMock, patch

import pytest

from notetaker import transcriber
from notetaker.transcriber import _gemini_call


def _response(text):
    resp = MagicMock()
    resp.text = text
    return resp


def _busy():
    return RuntimeError(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is "
        "currently experiencing high demand.', 'status': 'UNAVAILABLE'}}"
    )


def test_survives_the_2026_09_08_outage_shape():
    """Five straight 503s in a row, then success. The old loop died at three."""
    client = MagicMock()
    client.models.generate_content.side_effect = [
        _busy(),
        _busy(),
        _busy(),
        _busy(),
        _busy(),
        _response("notes"),
    ]

    with patch("notetaker.transcriber.time.sleep"):
        assert _gemini_call(client, ["prompt"]) == "notes"


def test_total_retry_window_covers_minutes_not_seconds():
    """Summed sleeps across a full failure run must span at least 5 minutes."""
    client = MagicMock()
    client.models.generate_content.side_effect = _busy()

    with (
        patch("notetaker.transcriber.time.sleep") as sleep,
        pytest.raises(RuntimeError),
    ):
        _gemini_call(client, ["prompt"])

    total = sum(call.args[0] for call in sleep.call_args_list)
    assert total >= 300, f"retry window is {total}s; the 09-08 spike ran >150s"


def test_heartbeat_is_kept_fresh_while_waiting_out_an_outage():
    """The healthcheck marks the container dead after ~170s of silence.

    A 120s backoff sleep must therefore be broken into slices with a heartbeat
    between them, and every slice must be shorter than the healthcheck window.
    """
    client = MagicMock()
    client.models.generate_content.side_effect = [_busy()] * 6 + [_response("ok")]
    beats = MagicMock()

    with patch("notetaker.transcriber.time.sleep") as sleep:
        assert _gemini_call(client, ["prompt"], heartbeat_fn=beats) == "ok"

    assert beats.call_count >= 6, "no heartbeat during retry sleeps"
    longest = max(call.args[0] for call in sleep.call_args_list)
    assert longest <= 60, f"a single {longest}s sleep would trip the healthcheck"


def test_non_transient_errors_still_fail_fast():
    """A bad API key must not burn six minutes of retries."""
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError(
        "400 INVALID_ARGUMENT. API key not valid"
    )

    with (
        patch("notetaker.transcriber.time.sleep") as sleep,
        pytest.raises(RuntimeError),
    ):
        _gemini_call(client, ["prompt"])

    assert client.models.generate_content.call_count == 1
    sleep.assert_not_called()


def test_heartbeat_reaches_the_segment_calls(tmp_path):
    """transcribe_and_summarize already takes heartbeat_fn; it must hand it
    down to the Gemini calls so long retries inside a segment also beat."""
    client = MagicMock()
    client.models.generate_content.side_effect = [_busy(), _busy(), _response("text")]
    beats = MagicMock()
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x")

    def fake_ffmpeg(args, **kw):
        for a in reversed(args):
            if isinstance(a, str) and a.endswith(".mp3"):
                with open(a, "wb") as f:
                    f.write(b"mp3")
                break
        return MagicMock(returncode=0)

    with (
        patch("notetaker.transcriber.genai") as genai,
        patch("notetaker.transcriber.subprocess.run", fake_ffmpeg),
        patch("notetaker.transcriber._get_audio_duration", return_value=120.0),
        patch("notetaker.transcriber.time.sleep"),
    ):
        genai.Client.return_value = client
        out = transcriber.transcribe_and_summarize(
            "key", str(audio), "Short call", heartbeat_fn=beats
        )

    assert out == "text"
    assert beats.call_count >= 2, "retry sleeps inside a segment did not beat"
