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


def test_retain_never_overwrites_an_earlier_recording_of_the_same_room(tmp_path):
    """Two failed calls in one room on one day produce the same audio name
    (<date>-<slug>.webm), so the second retain() used to shutil.move over the
    first recording and its sidecar without a word."""
    first = _retain(tmp_path, _meta(call_start="2026-09-08T09:01:39"), now=1.0)
    (tmp_path / "work" / "2026-09-08-accounting-talk.webm").write_bytes(b"second")
    second = retained.retain(
        str(tmp_path / "retained"),
        str(tmp_path / "work" / "2026-09-08-accounting-talk.webm"),
        _meta(call_start="2026-09-08T16:01:39"),
        now=2.0,
    )

    assert first != second
    assert os.path.basename(second) == "itujzrjw-2026-09-08-accounting-talk-160139.webm"
    with open(first, "rb") as f:
        assert f.read() == b"audio"
    with open(second, "rb") as f:
        assert f.read() == b"second"
    assert _load(retained.sidecar_path(first))["created_at"] == 1.0
    assert _load(retained.sidecar_path(second))["created_at"] == 2.0
    assert len(retained.pending(str(tmp_path / "retained"))) == 2


def test_retain_disambiguates_without_a_call_start_and_never_collides(tmp_path):
    d = str(tmp_path / "retained")
    meta = {"token": "tok", "name": "A"}
    dests = set()
    for i in range(3):
        src = tmp_path / "2026-09-08-a.webm"
        src.write_bytes(b"x")
        with patch(
            "notetaker.retained.time.localtime",
            return_value=(2026, 9, 8, 16, 1, 39, 0, 251, 0),
        ):
            dests.add(retained.retain(d, str(src), dict(meta), now=float(i)))
    assert len(dests) == 3
    assert len(retained.pending(d)) == 3


def test_retain_writes_the_sidecar_before_moving_the_audio(tmp_path):
    """If the sidecar write fails, the audio must still be where the log says
    it is (the working dir), not sitting in retained-audio/ without a sidecar
    where nothing will ever retry it and prune deletes it in 30 days."""
    src = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    src.parent.mkdir()
    src.write_bytes(b"audio")
    with (
        patch("notetaker.retained._atomic_write", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        retained.retain(str(tmp_path / "retained"), str(src), _meta(), now=1.0)
    assert src.exists()
    assert not os.listdir(tmp_path / "retained")


def test_retain_removes_the_sidecar_if_the_move_fails(tmp_path):
    """The reverse: a sidecar with no audio would just be dropped by drain()
    with a warning, hiding that the recording is still in tmpfs."""
    src = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    src.parent.mkdir()
    src.write_bytes(b"audio")
    with (
        patch("notetaker.retained.shutil.move", side_effect=OSError("cross-device")),
        pytest.raises(OSError),
    ):
        retained.retain(str(tmp_path / "retained"), str(src), _meta(), now=1.0)
    assert src.exists()
    assert not os.listdir(tmp_path / "retained")


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


def test_drain_removes_the_audio_before_the_sidecar(tmp_path):
    """A crash between the two unlinks must leave a sidecar without audio
    (which drain() drops on the next pass), never audio without a sidecar
    (which nothing looks at for 30 days)."""
    _retain(tmp_path, now=1000.0)
    order = []
    real_unlink = os.unlink

    def spy(path):
        order.append(os.path.basename(path))
        real_unlink(path)

    with patch("notetaker.retained.os.unlink", spy):
        retained.drain(
            str(tmp_path / "retained"), MagicMock(return_value=True), now=1e9
        )

    assert order == [
        "itujzrjw-2026-09-08-accounting-talk.webm",
        "itujzrjw-2026-09-08-accounting-talk.webm.json",
    ]


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


def test_drain_commits_the_attempt_before_running_it(tmp_path):
    """A process killed mid-retry (an OOM kill is SIGKILL, not an exception)
    must not replay the same attempt number forever, and make_notes reads the
    count to decide when Whisper has had enough chances."""
    dest = _retain(tmp_path, now=1000.0)
    seen = {}

    def process(meta, audio):
        seen["memory"] = meta["attempts"]
        seen["disk"] = _load(retained.sidecar_path(dest))["attempts"]
        seen["next"] = _load(retained.sidecar_path(dest))["next_attempt_at"]
        raise RuntimeError("killed")

    later = 1000.0 + 24 * 3600
    retained.drain(str(tmp_path / "retained"), process, now=later)

    assert seen == {"memory": 1, "disk": 1, "next": later + retained._backoff_for(1)}
    sidecar = _load(retained.sidecar_path(dest))
    assert sidecar["attempts"] == 1
    assert "killed" in sidecar["last_error"]


def test_drain_retries_at_most_one_recording_per_call(tmp_path):
    """Each retry blocks the poll loop for minutes; never chain them.

    "Oldest first" is by created_at. The filename starts with the room
    token, so sorting by name would let a room called "aaaa" starve one
    called "zzzz" no matter how long the latter has waited."""
    _retain(tmp_path, _meta(token="zzzz"), now=1000.0)
    _retain(tmp_path, _meta(token="aaaa"), now=1001.0)
    process = MagicMock(return_value=True)

    done, remaining = retained.drain(
        str(tmp_path / "retained"), process, now=1000.0 + 24 * 3600
    )

    assert process.call_count == 1
    assert (done, remaining) == (1, 1)
    assert process.call_args.args[0]["token"] == "zzzz", "oldest first"


def test_pending_orders_by_created_at_and_tolerates_a_bad_sidecar(tmp_path):
    d = tmp_path / "retained"
    _retain(tmp_path, _meta(token="zzzz"), now=1000.0)
    _retain(tmp_path, _meta(token="aaaa"), now=1001.0)
    (d / "mmmm-2026-09-01-call.webm.json").write_text("{not json")

    names = [os.path.basename(p) for p in retained.pending(str(d))]

    assert names[0].startswith("zzzz-")
    assert names[1].startswith("aaaa-")
    assert names[2].startswith("mmmm-"), "unreadable sidecars sort last, by name"


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
    """participants.get_participant_emails() returns [] when Nextcloud is
    down — it never raises. So the sidecar of a call that failed while
    Nextcloud was out carries participants=[] with attendee_ids present, and
    the retry must look them up again rather than treat [] as final. Before
    2026-09-14 it only re-resolved on None: the retry uploaded the notes,
    deleted the recording and emailed nobody, ever."""
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(return_value=(str(audio), [], {"brantley"}))
    mock_recorder_cls.return_value = mock_recorder
    mock_participants.return_value = []  # Nextcloud down: the real contract
    mock_transcribe.return_value = "# Notes"
    mock_upload.side_effect = RuntimeError("Nextcloud 502")  # still down

    asyncio.run(
        handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
    )

    dest = os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-08-accounting-talk.webm"
    )
    assert _load(retained.sidecar_path(dest))["participants"] == []
    mock_email.assert_not_called()

    # Nextcloud is back.
    mock_participants.reset_mock()
    mock_participants.return_value = [
        {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"}
    ]
    mock_upload.side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    mock_participants.assert_called_once()
    assert mock_participants.call_args.kwargs["attendee_ids"] == {"brantley"}
    mock_email.assert_called_once()
    assert [r["email"] for r in mock_email.call_args.args[1]["recipients"]] == [
        "b@x.com"
    ]
    assert not os.path.exists(dest)


@patch("notetaker.main.deliver_notes")
@patch("notetaker.main.upload_notes")
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize")
@patch("notetaker.main.AudioRecorder")
def test_retry_does_not_re_resolve_when_nobody_attended(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    tmp_path,
):
    """[] with no attendee_ids is a real answer (nobody to email), not a
    failed lookup; the retry must not fall back to room membership."""
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    mock_recorder = MagicMock()
    mock_recorder.record_call = AsyncMock(return_value=(str(audio), [], set()))
    mock_recorder_cls.return_value = mock_recorder
    mock_participants.return_value = []
    mock_transcribe.return_value = "# Notes"
    mock_upload.side_effect = RuntimeError("Nextcloud 502")

    asyncio.run(
        handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
    )

    mock_participants.reset_mock()
    mock_upload.side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    mock_participants.assert_not_called()
    mock_email.assert_not_called()


# --- I2: a retry must not redo work the live call already finished ------------


@patch("notetaker.main.deliver_notes", return_value=True)
@patch("notetaker.main.upload_notes", side_effect=RuntimeError("Nextcloud 502"))
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize", return_value="# Notes")
@patch("notetaker.main.AudioRecorder")
def test_retry_reuses_the_notes_and_does_not_email_twice(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    tmp_path,
):
    """Upload failed but the email went out. The recording is retained (the
    notes are not in durable storage), but the retry must only redo the
    upload: transcribing again costs Gemini quota and yields different text,
    and emailing again sends every attendee a duplicate on every attempt."""
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], {"brantley"}))
    mock_recorder_cls.return_value = rec
    mock_participants.return_value = [
        {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"}
    ]

    asyncio.run(
        handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
    )
    assert mock_email.call_count == 1

    dest = os.path.join(
        cfg.retained_audio_dir, "itujzrjw-2026-09-08-accounting-talk.webm"
    )
    sidecar = _load(retained.sidecar_path(dest))
    assert sidecar["notes"] == "# Notes"
    assert sidecar["email_done"] is True

    # Still down at the first retry: nothing is redone, the sidecar keeps
    # both facts for the next one.
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)
    assert mock_transcribe.call_count == 1
    assert mock_email.call_count == 1
    sidecar = _load(retained.sidecar_path(dest))
    assert sidecar["notes"] == "# Notes" and sidecar["email_done"] is True
    assert sidecar["attempts"] == 1

    # Nextcloud back: one upload, nothing else, recording gone.
    mock_upload.reset_mock()
    mock_upload.side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9 + 1e6):
        drain_retained(cfg)

    mock_upload.assert_called_once()
    assert mock_upload.call_args.args[-1] == "# Notes"
    assert mock_transcribe.call_count == 1
    assert mock_email.call_count == 1
    assert not os.path.exists(dest)
    assert not os.path.exists(retained.sidecar_path(dest))


@patch("notetaker.main.deliver_notes", return_value=False)
@patch("notetaker.main.upload_notes", side_effect=RuntimeError("Nextcloud 502"))
@patch("notetaker.main.get_participant_emails")
@patch("notetaker.main.transcribe_and_summarize", return_value="# Notes")
@patch("notetaker.main.AudioRecorder")
def test_a_spooled_email_also_counts_as_done(
    mock_recorder_cls,
    mock_transcribe,
    mock_participants,
    mock_upload,
    mock_email,
    tmp_path,
):
    """deliver_notes() returning False means the email is in the spool, which
    retries it on its own. The retained retry must not queue a second copy."""
    from notetaker.main import drain_retained, handle_call

    cfg = _cfg(tmp_path)
    audio = tmp_path / "work" / "2026-09-08-accounting-talk.webm"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    rec = MagicMock()
    rec.record_call = AsyncMock(return_value=(str(audio), [], {"brantley"}))
    mock_recorder_cls.return_value = rec
    mock_participants.return_value = [
        {"user_id": "brantley", "display_name": "Brantley Gunn", "email": "b@x.com"}
    ]

    asyncio.run(
        handle_call(cfg, {"token": "itujzrjw", "displayName": "Accounting Talk"})
    )
    assert mock_email.call_count == 1
    mock_upload.reset_mock()
    mock_upload.side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    assert mock_email.call_count == 1
    mock_upload.assert_called_once()


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
