"""The verbatim transcript: kept privately beside the notes, never emailed."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notetaker import retained, transcripts
from notetaker.config import Config
from tests.notes_fixture import NOTES_BODY

SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "Hello there."},
    {"start": 30.0, "end": 33.0, "text": "The budget is 4,200 dollars, not 4,000."},
]
TRANSCRIPT_LINES = [
    "[00:00] Hello there.",
    "[00:30] The budget is 4,200 dollars, not 4,000.",
]
NOTES = "# Notes\n\nSubject: Recap\n\nbody" + NOTES_BODY
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
        "/x/../meeting-notes/y",
        "/meeting-notes%2Fprivate",
        "/meeting-notes\\private",
        "/meeting-notes/private?x",
        "/private\x01",
        "/meeting-notes /x",
        "/meeting-notes\u200b/x",
        "/meeting-notes./x",
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


@pytest.mark.parametrize(
    "folder",
    ["/meeting-notes", "/meeting-notes/transcriptions", "/meeting-notes/summaries/x"],
)
def test_no_share_on_a_common_parent_could_expose_the_transcripts(tmp_path, folder):
    """The check sees names, not shares. With the summaries in a subfolder of
    a shared folder, a transcript anywhere under that folder would leak."""
    cfg = _cfg(
        tmp_path, notes_folder="/meeting-notes/summaries", transcripts_folder=folder
    )
    assert any("TRANSCRIPTS_FOLDER" in p for p in cfg.validate())


def test_unicode_variants_of_the_notes_folder_are_caught(tmp_path):
    nfc, nfd = "/caf\u00e9", "/cafe\u0301/x"
    cfg = _cfg(tmp_path, notes_folder=nfc, transcripts_folder=nfd)
    assert any("TRANSCRIPTS_FOLDER" in p for p in cfg.validate())


def test_mixing_storage_modes_is_refused_not_silently_ignored(tmp_path):
    local = _cfg(
        tmp_path,
        notes_storage="local",
        local_notes_dir="/data/notes",
        transcripts_folder="/meeting-notes-transcriptions",
    )
    assert any("TRANSCRIPTS_FOLDER" in p for p in local.validate())
    remote = _cfg(tmp_path, transcripts_folder="", local_transcripts_dir="/data/t")
    assert any("LOCAL_TRANSCRIPTS_DIR" in p for p in remote.validate())


def test_local_transcripts_may_not_live_where_the_notetaker_deletes(tmp_path):
    for folder in (
        str(tmp_path / "state" / "retained-audio" / "t"),
        str(tmp_path / "state" / "failed-email"),
        "/tmp/notetaker-audio/t",
    ):
        cfg = _cfg(
            tmp_path,
            transcripts_folder="",
            notes_storage="local",
            local_notes_dir="/data/notes",
            local_transcripts_dir=folder,
        )
        assert any("LOCAL_TRANSCRIPTS_DIR" in p for p in cfg.validate()), folder


def test_local_transcripts_dir_must_differ_but_may_be_a_subfolder(tmp_path):
    same = _cfg(
        tmp_path,
        transcripts_folder="",
        notes_storage="local",
        local_notes_dir="/data/notes",
        local_transcripts_dir="/data/notes/",
    )
    assert any("LOCAL_TRANSCRIPTS_DIR" in p for p in same.validate())
    sub = _cfg(
        tmp_path,
        transcripts_folder="",
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


def test_an_unsafe_folder_is_refused_even_with_preflight_skipped(
    tmp_path, whisper_pipeline
):
    """SKIP_PREFLIGHT skips validate(); the save itself must still refuse."""
    cfg = _cfg(
        tmp_path,
        notes_folder="/meeting-notes",
        transcripts_folder="/meeting-notes/private",
    )
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)

    whisper_pipeline["transcript_upload"].assert_not_called()
    whisper_pipeline["notes_upload"].assert_called_once()
    assert os.path.exists(_dest(cfg))  # kept until the folder is fixed


def test_the_transcript_is_kept_even_when_the_notes_step_fails(
    tmp_path, whisper_pipeline
):
    """Whisper finished, the summary did not. The transcript is stored now,
    not left in a sidecar the 30-day prune deletes; the retry then stores the
    notes under the same name and does not upload the transcript again."""
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with (
        patch(
            "notetaker.transcriber.summarize_transcript",
            side_effect=RuntimeError("quota"),
        ),
        patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)),
        pytest.raises(RuntimeError),
    ):
        _run_call(cfg)

    whisper_pipeline["transcript_upload"].assert_called_once()
    transcript_name = whisper_pipeline["transcript_upload"].call_args.args[4]
    whisper_pipeline["notes_upload"].assert_not_called()
    whisper_pipeline["email"].assert_not_called()
    assert os.path.exists(_dest(cfg))

    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    whisper_pipeline["notes_upload"].assert_called_once()
    assert whisper_pipeline["notes_upload"].call_args.args[4] == transcript_name
    whisper_pipeline["transcript_upload"].assert_called_once()
    whisper_pipeline["email"].assert_called_once()
    assert whisper_pipeline["whisper"].call_count == 1
    assert not os.path.exists(_dest(cfg))


def test_a_retry_keeps_the_name_the_first_file_was_stored_under(
    tmp_path, whisper_pipeline
):
    """The live call's recipient lookup failed (names 'unknown'); the retry
    resolves real names. The pair must still share one filename."""
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    whisper_pipeline["notes_upload"].side_effect = RuntimeError("Nextcloud 502")
    with (
        patch("notetaker.main.get_participant_emails", return_value=[]),
        patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)),
    ):
        _run_call(cfg)
    transcript_name = whisper_pipeline["transcript_upload"].call_args.args[4]
    assert transcript_name.endswith("-unknown.md")

    whisper_pipeline["notes_upload"].side_effect = None
    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    assert whisper_pipeline["notes_upload"].call_args.args[4] == transcript_name


def test_a_transcript_retry_uses_the_name_the_notes_were_saved_under(
    tmp_path, whisper_pipeline
):
    """Notes saved, transcript upload failed; the retry resolves different
    participants. The transcript must still take the summary's name."""
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    whisper_pipeline["transcript_upload"].side_effect = RuntimeError("502")
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)
    notes_name = whisper_pipeline["notes_upload"].call_args.args[4]

    whisper_pipeline["transcript_upload"].side_effect = None
    with (
        patch("notetaker.retained.time.time", return_value=9e9),
        patch(
            "notetaker.main.get_participant_emails",
            return_value=[
                {"user_id": "zed", "display_name": "Zed", "email": "z@x.com"}
            ],
        ),
    ):
        # Force the retry to look the recipients up again.
        dest = _dest(cfg)
        with open(retained.sidecar_path(dest), encoding="utf-8") as f:
            sidecar = json.load(f)
        sidecar["participants"] = None
        with open(retained.sidecar_path(dest), "w", encoding="utf-8") as f:
            json.dump(sidecar, f)
        drain_retained(cfg)

    assert whisper_pipeline["transcript_upload"].call_args.args[4] == notes_name


def test_a_failed_lookup_does_not_pin_an_unknown_name_when_notes_fail(
    tmp_path, whisper_pipeline
):
    """Recipient lookup failed AND the notes step failed: the transcript waits
    for the retry, which resolves real names for both files."""
    from notetaker.main import drain_retained

    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with (
        patch("notetaker.main.get_participant_emails", return_value=[]),
        patch(
            "notetaker.transcriber.summarize_transcript",
            side_effect=RuntimeError("quota"),
        ),
        patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)),
        pytest.raises(RuntimeError),
    ):
        _run_call(cfg)
    whisper_pipeline["transcript_upload"].assert_not_called()

    with patch("notetaker.retained.time.time", return_value=9e9):
        drain_retained(cfg)

    name = whisper_pipeline["transcript_upload"].call_args.args[4]
    assert name == whisper_pipeline["notes_upload"].call_args.args[4]
    assert name.endswith("-brantley-jo.md")


def test_a_broken_transcript_save_never_hides_the_notes_error(
    tmp_path, whisper_pipeline
):
    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with (
        patch(
            "notetaker.transcriber.summarize_transcript",
            side_effect=RuntimeError("quota"),
        ),
        patch("notetaker.main._save_transcript", side_effect=KeyError("boom")),
        patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)),
        pytest.raises(RuntimeError, match="quota"),
    ):
        _run_call(cfg)


def test_the_email_goes_out_before_the_transcript_upload(tmp_path, whisper_pipeline):
    order = []
    whisper_pipeline["email"].side_effect = lambda *a, **k: order.append("email")
    whisper_pipeline["transcript_upload"].side_effect = lambda *a, **k: order.append(
        "transcript"
    )
    cfg = _cfg(tmp_path)
    audio = _audio(tmp_path)
    with patch("notetaker.main.AudioRecorder", return_value=_recorder(audio)):
        _run_call(cfg)
    assert order == ["email", "transcript"]


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
        transcripts_folder="",
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


def test_reprocess_out_writes_no_transcript_when_transcripts_are_off(
    tmp_path, monkeypatch
):
    from notetaker import reprocess

    _reprocess_env(monkeypatch, transcripts_folder="")
    audio = _retained_with_transcript(tmp_path)
    out = tmp_path / "recovered.md"
    with (
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
    ):
        assert reprocess.main([str(audio), "--out", str(out)]) == 0

    assert out.read_text() == NOTES
    assert not (tmp_path / "recovered-transcript.md").exists()


def test_reprocess_refuses_an_unsafe_transcripts_folder(tmp_path, monkeypatch):
    from notetaker import reprocess

    _reprocess_env(monkeypatch, transcripts_folder="/meeting-notes-summaries/t")
    audio = _retained_with_transcript(tmp_path)
    with (
        patch("notetaker.transcriber._get_audio_duration", return_value=60.0),
        patch("notetaker.transcriber.summarize_transcript", return_value=NOTES),
        patch("notetaker.reprocess.upload_notes"),
        patch("notetaker.transcripts.upload_notes") as transcript_upload,
        pytest.raises(RuntimeError, match="TRANSCRIPTS_FOLDER"),
    ):
        reprocess.main([str(audio)])
    transcript_upload.assert_not_called()


# --- preflight ------------------------------------------------------------------


def _propfind(status):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    return resp


def test_preflight_fails_when_the_transcripts_folder_is_missing(tmp_path):
    from notetaker.preflight import check_transcripts_target

    cfg = _cfg(tmp_path)
    with (
        patch("notetaker.preflight.requests.request", return_value=_propfind(404)),
        pytest.raises(RuntimeError, match="does not exist"),
    ):
        check_transcripts_target(cfg)
    with patch(
        "notetaker.preflight.requests.request", return_value=_propfind(207)
    ) as req:
        check_transcripts_target(cfg)
    assert req.call_args.args[1].endswith("/files/bot/meeting-notes-transcriptions")


@pytest.mark.parametrize("status", [401, 403, 500])
def test_preflight_fails_on_any_other_webdav_error(tmp_path, status):
    from notetaker.preflight import check_transcripts_target

    with (
        patch("notetaker.preflight.requests.request", return_value=_propfind(status)),
        pytest.raises(RuntimeError, match=str(status)),
    ):
        check_transcripts_target(_cfg(tmp_path))


def test_preflight_fails_on_a_read_only_share(tmp_path):
    from notetaker.preflight import check_transcripts_target

    resp = _propfind(207)
    resp.text = "<d:multistatus><oc:permissions>SG</oc:permissions></d:multistatus>"
    with (
        patch("notetaker.preflight.requests.request", return_value=resp),
        pytest.raises(RuntimeError, match="read-only"),
    ):
        check_transcripts_target(_cfg(tmp_path))
    resp.text = (
        "<d:multistatus><oc:permissions>SGDNVCK</oc:permissions></d:multistatus>"
    )
    with patch("notetaker.preflight.requests.request", return_value=resp):
        check_transcripts_target(_cfg(tmp_path))


def test_preflight_runs_the_transcripts_check_only_when_enabled(tmp_path):
    from notetaker import preflight

    names = []

    def record(name):
        return lambda cfg: names.append(name)

    with patch.multiple(
        preflight,
        check_state_dir=record("state-dir"),
        check_smtp=record("smtp"),
        check_nextcloud=record("nextcloud"),
        check_notes_target=record("notes-target"),
        check_transcripts_target=record("transcripts-target"),
        check_gemini=record("gemini"),
        check_claude=record("claude"),
        check_whisper=record("whisper"),
    ):
        preflight.run_preflight(_cfg(tmp_path))
        assert "transcripts-target" in names
        names.clear()
        preflight.run_preflight(_cfg(tmp_path, transcripts_folder=""))
        assert "transcripts-target" not in names
