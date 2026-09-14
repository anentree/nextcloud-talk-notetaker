"""A retained recording must be retried on its own, not wait for a human.

Before 2026-09-14, main.py moved a recording whose notes could not be made
into retained-audio/ and logged "for manual reprocessing". Nothing ever
looked at that directory again: the 2026-09-08 Accounting Talk recording
sat there for six days with everything needed to finish it (recipients,
speaker timeline, call start) already thrown away. retained.py keeps that
context in a sidecar and the poll loop drains the queue with backoff, the
same way spool.py already does for email.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notetaker import retained
from notetaker.config import Config


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _meta(**overrides):
    meta = {
        "token": "itujzrjw",
        "name": "Accounting Talk",
        "call_start": "2026-09-08T16:01:39",
        "speaker_events": [{"t": 1, "track": "2:janus0", "on": True}],
        "attendee_ids": ["brantley", "piyush"],
        "participants": [
            {
                "user_id": "brantley",
                "display_name": "Brantley Gunn",
                "email": "b@x.com",
            },
        ],
    }
    meta.update(overrides)
    return meta


def _retain(tmp_path, meta=None, now=1000.0):
    src = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"audio")
    return retained.retain(
        str(tmp_path / "retained"), str(src), meta or _meta(), now=now
    )


# --- retain() -----------------------------------------------------------------


def test_retain_moves_audio_and_writes_sidecar_with_everything_needed(tmp_path):
    dest = _retain(tmp_path)

    assert os.path.exists(dest)
    assert not os.path.exists(tmp_path / "work" / "2026-09-08-accounting-talk.webm")
    assert os.path.basename(dest) == "itujzrjw-2026-09-08-accounting-talk.webm"

    sidecar = _load(retained.sidecar_path(dest))
    for key in (
        "token",
        "name",
        "call_start",
        "speaker_events",
        "attendee_ids",
        "participants",
    ):
        assert sidecar[key] == _meta()[key], key
    assert sidecar["attempts"] == 0
    assert sidecar["created_at"] == 1000.0
    assert sidecar["next_attempt_at"] > 1000.0, "must not retry in the same outage"


def test_retain_keeps_the_old_filename_shape(tmp_path):
    """reprocess.py parses <token>-<date>-<slug>.webm; do not break it."""
    dest = _retain(tmp_path)
    from notetaker.reprocess import parse_retained_name

    assert parse_retained_name(dest) == ("itujzrjw", "2026-09-08", "Accounting Talk")


# --- drain() ------------------------------------------------------------------


def test_drain_skips_entries_not_yet_due(tmp_path):
    _retain(tmp_path, now=1000.0)
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(str(tmp_path / "retained"), process, now=1001.0)

    process.assert_not_called()
    assert (done, remaining) == (0, 1)


def test_drain_retries_and_removes_on_success(tmp_path):
    dest = _retain(tmp_path, now=1000.0)
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(
        str(tmp_path / "retained"), process, now=1000.0 + 24 * 3600
    )

    assert (done, remaining) == (1, 0)
    meta, audio = process.call_args.args
    assert audio == dest
    assert meta["token"] == "itujzrjw"
    assert meta["participants"][0]["email"] == "b@x.com"
    assert not os.path.exists(dest)
    assert not os.path.exists(retained.sidecar_path(dest))


def test_drain_backs_off_when_the_retry_fails_again(tmp_path):
    dest = _retain(tmp_path, now=1000.0)
    process = MagicMock(side_effect=RuntimeError("503 UNAVAILABLE"))
    later = 1000.0 + 24 * 3600

    done, remaining = retained.drain(str(tmp_path / "retained"), process, now=later)

    assert (done, remaining) == (0, 1)
    assert os.path.exists(dest), "a failed retry must never lose the recording"
    sidecar = _load(retained.sidecar_path(dest))
    assert sidecar["attempts"] == 1
    assert "503" in sidecar["last_error"]
    assert sidecar["next_attempt_at"] > later

    # Second failure waits longer than the first.
    first_gap = sidecar["next_attempt_at"] - later
    later2 = sidecar["next_attempt_at"] + 1
    retained.drain(str(tmp_path / "retained"), process, now=later2)
    sidecar = _load(retained.sidecar_path(dest))
    assert sidecar["attempts"] == 2
    assert sidecar["next_attempt_at"] - later2 > first_gap


def test_drain_treats_a_false_return_as_failure(tmp_path):
    """process_fn returns notes_saved; False means the notes are not safe."""
    dest = _retain(tmp_path, now=1000.0)
    process = MagicMock(return_value=False)

    retained.drain(str(tmp_path / "retained"), process, now=1000.0 + 24 * 3600)

    assert os.path.exists(dest)
    assert _load(retained.sidecar_path(dest))["attempts"] == 1


def test_drain_retries_at_most_one_recording_per_call(tmp_path):
    """Each retry blocks the poll loop for minutes; never chain them."""
    _retain(tmp_path, _meta(token="aaaa"), now=1000.0)
    _retain(tmp_path, _meta(token="bbbb"), now=1001.0)
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(
        str(tmp_path / "retained"), process, now=1000.0 + 24 * 3600
    )

    assert process.call_count == 1
    assert (done, remaining) == (1, 1)
    assert process.call_args.args[0]["token"] == "aaaa", "oldest first"


def test_drain_drops_a_sidecar_whose_audio_was_pruned(tmp_path):
    dest = _retain(tmp_path, now=1000.0)
    os.unlink(dest)
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(
        str(tmp_path / "retained"), process, now=1000.0 + 24 * 3600
    )

    process.assert_not_called()
    assert (done, remaining) == (0, 0)
    assert not os.path.exists(retained.sidecar_path(dest))


def test_drain_ignores_legacy_recordings_without_a_sidecar(tmp_path, caplog):
    """Files retained before sidecars existed still need a human; say so once."""
    d = tmp_path / "retained"
    d.mkdir()
    (d / "92gwpi2v-2026-08-25-old-call.webm").write_bytes(b"audio")
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(str(d), process, now=5000.0)

    process.assert_not_called()
    assert (done, remaining) == (0, 0)
    assert os.path.exists(d / "92gwpi2v-2026-08-25-old-call.webm")


def test_drain_survives_an_unreadable_sidecar(tmp_path):
    d = tmp_path / "retained"
    d.mkdir()
    (d / "x-2026-09-01-call.webm").write_bytes(b"audio")
    (d / "x-2026-09-01-call.webm.json").write_text("{not json")
    process = MagicMock(return_value=True)

    done, _ = retained.drain(str(d), process, now=5000.0)

    process.assert_not_called()
    assert done == 0


# --- main.py wiring -----------------------------------------------------------


def _cfg(tmp_path):
    return Config(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        state_dir=str(tmp_path / "state"),
        heartbeat_file=str(tmp_path / "hb"),
    )


@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_failed_call_is_retained_with_its_recipients_and_later_finished(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    tmp_path,
):
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")

    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(
        return_value=(str(audio), [{"t": 1}], {"brantley", "piyush"})
    )
    mock_recorder_cls.return_value = mock_recorder
    mock_participants.return_value = [
        {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"},
        {"user_id": "piyush", "display_name": "Piyush", "email": "p@y.com"},
    ]
    mock_transcribe.side_effect = RuntimeError("503 UNAVAILABLE")

    # 1. The live call fails at transcription: audio retained WITH context.
    with pytest.raises(RuntimeError):
        asyncio.run(
            handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
        )

    dest = os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-08-accounting-talk.webm"
    )
    assert os.path.exists(dest)
    sidecar = _load(retained.sidecar_path(dest))
    assert [p["email"] for p in sidecar["participants"]] == ["b@x.com", "p@y.com"]
    assert sidecar["speaker_events"] == [{"t": 1}]
    assert sidecar["name"] == "Accounting Talk"
    mock_upload.assert_not_called()
    mock_email.assert_not_called()

    # 2. Not due yet: nothing happens.
    mock_transcribe.reset_mock()
    mock_transcribe.side_effect = None
    mock_transcribe.return_value = "# Notes\n\nSubject: hi\n\nbody"
    drain_retained(cfg)
    mock_upload.assert_not_called()

    # 3. Due: transcribed with the saved context, stored, emailed, cleaned up.
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    mock_transcribe.assert_called_once()
    kwargs = mock_transcribe.call_args.kwargs
    assert kwargs["speaker_events"] == [{"t": 1}]
    assert kwargs["participant_names"] == ["Brantley Gunn", "Piyush"]
    mock_upload.assert_called_once()
    mock_email.assert_called_once()
    envelope = mock_email.call_args.args[1]
    assert [r["email"] for r in envelope["recipients"]] == ["b@x.com", "p@y.com"]
    assert envelope["room"] == "itujzrjw"
    assert not os.path.exists(dest)
    assert not os.path.exists(retained.sidecar_path(dest))
    # Participants were resolved once, during the live call - not again.
    mock_participants.assert_called_once()


@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_retry_resolves_recipients_if_the_live_call_died_before_it_could(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    tmp_path,
):
    """If Nextcloud itself was down, the sidecar has attendee_ids but no
    participants; the retry must look them up rather than email nobody."""
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(return_value=(str(audio), [], {"brantley"}))
    mock_recorder_cls.return_value = mock_recorder
    mock_participants.side_effect = RuntimeError("Nextcloud 502")

    with pytest.raises(RuntimeError):
        asyncio.run(
            handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
        )

    dest = os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-08-accounting-talk.webm"
    )
    assert _load(retained.sidecar_path(dest))["participants"] is None

    mock_participants.side_effect = None
    mock_participants.return_value = [
        {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"}
    ]
    mock_transcribe.return_value = "# Notes"
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    assert mock_participants.call_args.kwargs["attendee_ids"] == {"brantley"}
    mock_email.assert_called_once()
    assert not os.path.exists(dest)


def test_prune_removes_the_sidecar_with_the_audio(tmp_path):
    from notetaker.main import prune_retained_audio

    cfg = _cfg(tmp_path)
    dest = _retain(tmp_path, now=1000.0)
    os.makedirs(cfg.retained_audio_dir, exist_ok=True)
    for p in (dest, retained.sidecar_path(dest)):
        target = os.path.join(cfg.retained_audio_dir, os.path.basename(p))
        os.rename(p, target)
        os.utime(target, (0, 0))

    assert prune_retained_audio(cfg) == 2
    assert not os.listdir(cfg.retained_audio_dir)
