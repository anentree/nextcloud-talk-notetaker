from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime

from notetaker import retained, spool, transcripts
from notetaker.config import Config
from notetaker.mailer import extract_follow_up_email, send_notes_email
from notetaker.monitor import CallMonitor
from notetaker.participants import get_participant_emails
from notetaker.preflight import run_preflight
from notetaker.recorder import AudioRecorder
from notetaker.storage import upload_notes
from notetaker.transcriber import NO_SPEECH_SENTINEL, make_notes

log = logging.getLogger(__name__)

EMAIL_ATTEMPTS = 3
EMAIL_RETRY_DELAY = 10
# A recording still in AUDIO_DIR this long after last being written belongs to
# a call that died without cleaning up. Well beyond any real meeting.
STALE_AUDIO_SECONDS = 12 * 3600


def _send_envelope(cfg: Config, envelope: dict) -> dict:
    """Send one email envelope.

    Raises if the message could not be handed to the mail server at all.
    Returns a residual envelope containing only the recipients the server
    refused, or an empty dict when everyone was accepted. A retry must use
    the residual, never the original — otherwise the people who already
    received the notes get another copy on every attempt.
    """
    recipients = envelope["recipients"]
    accepted = send_notes_email(
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_from=cfg.smtp_from or f"{cfg.nextcloud_user}@localhost",
        smtp_user=cfg.smtp_user,
        smtp_password=cfg.smtp_password,
        recipients=recipients,
        subject=envelope["subject"],
        body=envelope["body"],
        notes_markdown=envelope["notes"],
        attachment_filename=envelope.get("filename", "meeting-notes.md"),
        timeout=cfg.smtp_timeout,
        tls_verify=cfg.smtp_tls_verify,
    )

    accepted_set = set(accepted)
    remaining = [r for r in recipients if r.get("email") not in accepted_set]
    if not remaining:
        return {}
    return dict(envelope, recipients=remaining)


def deliver_notes(cfg: Config, envelope: dict) -> bool:
    """Send the notes email, retrying, then spooling it if it still fails.

    Returns True if every recipient was reached. A False return is never
    silent: whatever could not be delivered is on disk and drain_spool()
    keeps retrying it.
    """
    pending = envelope
    last_exc: Exception | None = None

    for attempt in range(1, EMAIL_ATTEMPTS + 1):
        # Retries plus SMTP timeouts can span minutes; keep proving liveness.
        _touch_heartbeat(cfg)
        try:
            residual = _send_envelope(cfg, pending)
        except Exception as exc:
            last_exc = exc
            log.warning(
                "Email send failed (attempt %d/%d): %s: %s",
                attempt,
                EMAIL_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
            if attempt < EMAIL_ATTEMPTS:
                time.sleep(EMAIL_RETRY_DELAY)
            continue

        if not residual:
            return True
        # Some addresses were refused. Keep only those for the next attempt.
        pending = residual
        last_exc = None
        log.warning(
            "Retrying %d refused recipient(s)", len(residual.get("recipients", []))
        )
        if attempt < EMAIL_ATTEMPTS:
            time.sleep(EMAIL_RETRY_DELAY)

    log.error(
        "Email delivery incomplete after %d attempts (%s) — queueing remainder",
        EMAIL_ATTEMPTS,
        last_exc,
    )
    try:
        spool.enqueue(cfg.spool_dir, pending)
    except Exception:
        log.exception(
            "CRITICAL: could not queue the undelivered email — notes are in "
            "Nextcloud but this email is lost"
        )
    return False


def drain_spool(cfg: Config) -> None:
    """Retry previously-undeliverable emails. Called every poll cycle."""
    try:
        spool.drain(cfg.spool_dir, lambda env: _send_envelope(cfg, env))
    except Exception:
        log.exception("Error draining the email spool")


def _build_notes_filename(
    participants: list[dict[str, str]],
    now: datetime,
    last_user: str = "",
) -> str:
    """Build filename: YYYY-MM-DD-HHMM-firstname-firstname.md

    Names are alphabetical. If last_user is set, that user is placed last.
    """
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    first_names = []
    has_last_user = False
    for p in participants:
        first = p["display_name"].split()[0].lower()
        first = re.sub(r"[^a-z0-9]", "", first)
        if last_user and p.get("user_id") == last_user:
            has_last_user = True
        else:
            first_names.append(first)

    first_names.sort()
    if has_last_user:
        first_names.append(last_user)

    names_slug = "-".join(first_names) if first_names else "unknown"
    return f"{date_str}-{time_str}-{names_slug}.md"


async def handle_call(cfg: Config, room: dict) -> None:
    token = room["token"]
    name = room.get("displayName", token)
    call_start = datetime.now()
    log.info("Processing call in room '%s' (%s)", name, token)

    # 1. Record audio
    recorder = AudioRecorder(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_web_password,
        cfg.audio_dir,
        auth_method=cfg.auth_method,
    )
    # A call blocks the outer poll loop, so the recorder keeps the heartbeat
    # fresh on our behalf.
    recorder.heartbeat_fn = lambda: _touch_heartbeat(cfg)
    audio_path, speaker_events, attendee_ids = await recorder.record_call(token, name)
    log.info("Captured %d speaker events for room '%s'", len(speaker_events), name)

    # Everything the rest of the pipeline needs, in one place, so that if it
    # fails the recording can be retained WITH this context and finished
    # later by drain_retained(). Before 2026-09-14 only the audio survived a
    # failure; the recipients and speaker timeline were computed and lost.
    meta = {
        "token": token,
        "name": name,
        "call_start": call_start.isoformat(),
        "speaker_events": speaker_events,
        "attendee_ids": sorted(attendee_ids),
        "participants": None,
    }

    # Everything from here on must leave the recording in a defined state:
    # deleted if the notes are safe, moved somewhere persistent if not. An
    # exception escaping in between (a Gemini outage, a bad display name)
    # would otherwise strand the file in tmpfs until the next restart.
    notes_saved = False
    try:
        notes_saved = await _process_recording(cfg, meta, audio_path)
    finally:
        if notes_saved:
            _discard_audio(audio_path)
        elif os.path.exists(audio_path):
            _retain_audio(cfg, audio_path, meta)


def _resolve_participants(cfg: Config, token: str, attendee_ids: set) -> list[dict]:
    """Recipients for a room: only people actually observed in the call. A
    room can have members who never joined; they must not receive the notes."""
    if not attendee_ids:
        log.warning(
            "No in-call attendees were observed for room %s — falling back to "
            "room membership for recipients",
            token,
        )
    participants = get_participant_emails(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        token,
        mail_domain=cfg.mail_domain,
        email_overrides=cfg.email_overrides,
        exclude_user=cfg.nextcloud_user,
        attendee_ids=attendee_ids or None,
    )
    log.info(
        "Recipients for room %s: %s",
        token,
        [(p["user_id"], p["email"]) for p in participants],
    )
    return participants


async def _process_recording(cfg: Config, meta: dict, audio_path: str) -> bool:
    """Transcribe, store and email one recording. Returns True if the notes
    reached durable storage (which is what decides whether the audio can be
    thrown away). Fills ``meta["participants"]`` as soon as they are known so
    a later failure retains them with the audio."""
    token, name = meta["token"], meta["name"]
    # 2. Check audio is not empty
    audio_size = os.path.getsize(audio_path)
    if audio_size == 0:
        log.warning("Empty audio file for room '%s' — skipping transcription", name)
        # Nothing to preserve: an empty file is not evidence of anything.
        return True
    log.info("Audio file: %s (%d KB)", audio_path, audio_size // 1024)

    # 3. Get participants (needed for filename and email)
    participants = _resolve_participants(
        cfg, token, set(meta.get("attendee_ids") or ())
    )
    meta["participants"] = participants

    return _finish_recording(cfg, meta, audio_path, participants)


def _finish_recording(
    cfg: Config, meta: dict, audio_path: str, participants: list[dict]
) -> bool:
    """Steps 4-7 of the pipeline, shared by the live call and the retained
    retry: transcribe, save, email, keep the transcript. Returns True once the
    notes (and the transcript, when one is kept) are durable."""
    token, name = meta["token"], meta["name"]
    call_start = datetime.fromisoformat(meta["call_start"])

    # 4. Transcribe + summarize
    # Transcribing a long call takes many minutes; keep the heartbeat fresh
    # so the container doesn't report unhealthy while it is doing exactly
    # what it is supposed to do.
    _touch_heartbeat(cfg)
    # Once either file is stored under this name, a retry must reuse it: a
    # retry may resolve different participants, and the transcript has to
    # stay paired with its summary.
    filename = meta.get("filename") or _build_notes_filename(
        participants, call_start, last_user=cfg.filename_last_user
    )
    notes = meta.get("notes")
    if notes:
        # A retry: the notes survived in the sidecar. Doing it again would
        # spend Gemini quota to produce different text.
        log.info("Reusing the notes from an earlier attempt for room %s", token)
    else:
        participant_names = [p["display_name"] for p in participants]
        try:
            notes = make_notes(
                cfg,
                meta,
                audio_path,
                name,
                # A retry runs days later; the notes are dated by the call.
                call_start.date().isoformat(),
                participant_names=participant_names,
                heartbeat_fn=lambda: _touch_heartbeat(cfg),
            )
        except Exception:
            # Whisper may have finished before the notes step failed. Store
            # the transcript now rather than leave it only in a sidecar that
            # the 30-day prune deletes if the notes never come right. Not
            # when the recipient lookup failed: that would pin an "-unknown"
            # filename the retry could otherwise fix.
            lookup_failed = not participants and meta.get("attendee_ids")
            if meta.get("transcript") is not None and not lookup_failed:
                try:
                    _save_transcript(cfg, meta, filename, participants)
                except Exception:
                    # Never let this replace the error that got us here.
                    log.exception("Could not save the transcript for room %s", token)
            raise
        meta["notes"] = notes
    _touch_heartbeat(cfg)

    # 5. Save notes
    notes_saved = bool(meta.get("notes_saved"))
    if notes_saved:
        # A retry for the transcript alone; the notes are already stored.
        log.info("Notes for room %s were saved by an earlier attempt", token)
    else:
        try:
            _save_notes(cfg, filename, notes)
            notes_saved = True
            meta["notes_saved"] = True
            meta["filename"] = filename
        except Exception:
            # Keep going: the email below is now the ONLY copy of these notes,
            # which makes delivering it more important, not less.
            log.exception("Failed to save notes for room %s", token)

    # 6. Email participants (retried, then spooled — never dropped)
    if not cfg.email_enabled:
        log.info(
            "Email is disabled (SMTP_HOST unset); not emailing notes for room %s",
            token,
        )
        delivered = False
    elif meta.get("email_done"):
        # A retry after the email already went out (or was spooled, which
        # retries itself). Sending again would mail everyone a duplicate.
        log.info("Email for room %s was already handled by an earlier attempt", token)
        delivered = True
    elif participants:
        subject, body = extract_follow_up_email(notes)
        delivered = deliver_notes(
            cfg,
            {
                "room": token,
                "room_name": name,
                "recipients": participants,
                "subject": subject,
                "body": body,
                "notes": notes,
                "filename": filename,
            },
        )
        # True or False, the email is now either sent or in the spool;
        # either way a retry of this recording must not send it again.
        meta["email_done"] = True
    else:
        log.warning("No participants with email found for room %s", token)
        delivered = False

    # 7. Save the word-for-word transcript — privately, never emailed. After
    # the email so a slow upload here cannot hold the notes back.
    transcript_saved = _save_transcript(cfg, meta, filename, participants)

    log.info(
        "Done processing call in room '%s' (notes_saved=%s, transcript_saved=%s, "
        "email_delivered=%s)",
        name,
        notes_saved,
        transcript_saved,
        delivered,
    )
    # The caller decides what to do with the audio based on this. Deleting the
    # recording at the exact moment the pipeline is known to be broken is how
    # the 2026-08-13 calls became unrecoverable. An unsaved transcript keeps
    # the recording too: its sidecar holds the transcript, so the retry only
    # redoes the upload.
    return notes_saved and transcript_saved


def _save_notes(cfg: Config, filename: str, notes: str) -> None:
    if cfg.notes_storage == "local" and cfg.local_notes_dir:
        os.makedirs(cfg.local_notes_dir, exist_ok=True)
        local_path = os.path.join(cfg.local_notes_dir, filename)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(notes)
        log.info("Saved notes locally to %s", local_path)
    else:
        upload_notes(
            cfg.nextcloud_url,
            cfg.nextcloud_user,
            cfg.nextcloud_password,
            cfg.notes_folder,
            filename,
            notes,
        )


def _save_transcript(
    cfg: Config, meta: dict, filename: str, participants: list[dict]
) -> bool:
    """Store the verbatim transcript in its private folder. Returns False only
    when there is a transcript to keep and it could not be stored."""
    transcript = meta.get("transcript")
    if not cfg.transcripts_enabled:
        return True
    if meta.get("transcript_saved"):
        return True
    if not transcript or transcript == NO_SPEECH_SENTINEL:
        log.info(
            "No verbatim transcript for room %s (%s); nothing to save",
            meta.get("token"),
            "no speech" if transcript else "none was produced for this recording",
        )
        return True
    document = transcripts.build_document(
        transcript,
        meta.get("name", meta.get("token", "")),
        datetime.fromisoformat(meta["call_start"]).strftime("%Y-%m-%d %H:%M"),
        # Without observed attendees the recipients are the room's members,
        # and a member list is not evidence anyone was on the call.
        [p["display_name"] for p in participants] if meta.get("attendee_ids") else None,
    )
    try:
        transcripts.save(cfg, filename, document)
    except Exception:
        log.exception("Failed to save the transcript for room %s", meta.get("token"))
        return False
    meta["transcript_saved"] = True
    meta["filename"] = filename
    return True


def _discard_audio(audio_path: str) -> None:
    try:
        os.unlink(audio_path)
        log.info("Cleaned up audio file: %s", audio_path)
    except OSError:
        log.warning("Failed to clean up audio file: %s", audio_path)


def _retain_audio(cfg: Config, audio_path: str, meta: dict) -> None:
    """Move the recording somewhere persistent, with its context, so
    drain_retained() can finish the call later without anyone's help.

    AUDIO_DIR is a tmpfs mount, so leaving the file in place would lose it on
    the next container restart.
    """
    token = meta.get("token", "unknown")
    try:
        dest = retained.retain(cfg.retained_audio_dir, audio_path, meta)
        log.error(
            "Notes or transcript could not be saved for room %s — recording retained at %s; "
            "it will be retried automatically (first try in %d min)",
            token,
            dest,
            retained.INITIAL_BACKOFF_SECONDS // 60,
        )
    except Exception:
        log.exception(
            "Failed to retain audio for room %s; leaving it at %s", token, audio_path
        )


def _retry_recording(cfg: Config, meta: dict, audio_path: str) -> bool:
    """Finish a retained recording. Recipients were normally resolved during
    the live call; if that step itself failed (Nextcloud down), do it now.

    get_participant_emails() answers [] on failure rather than raising, so
    an empty list with attendees on record is a failed lookup, not "nobody
    to email". Only an empty list with no attendees is a real answer."""
    participants = meta.get("participants")
    if participants is None or (not participants and meta.get("attendee_ids")):
        participants = _resolve_participants(
            cfg, meta["token"], set(meta.get("attendee_ids") or ())
        )
        meta["participants"] = participants
    return _finish_recording(cfg, meta, audio_path, participants)


def drain_retained(cfg: Config) -> None:
    """Retry one retained recording that is due. Called every idle poll cycle."""
    try:
        retained.drain(
            cfg.retained_audio_dir,
            lambda meta, audio: _retry_recording(cfg, meta, audio),
        )
    except Exception:
        log.exception("Error draining retained recordings")


def prune_retained_audio(cfg: Config) -> int:
    """Delete retained recordings past their retention window.

    Without this a sustained Nextcloud outage keeps every call's .webm
    forever on the notes volume.
    """
    removed = _prune_dir(
        cfg.retained_audio_dir, cfg.retained_audio_max_age_days * 86400, "retained"
    )
    # Also sweep the working directory. If record_call itself raises after a
    # partial flush, the caller never learns the path, so the fragment would
    # sit in tmpfs (RAM) until the container restarts.
    removed += _prune_dir(cfg.audio_dir, STALE_AUDIO_SECONDS, "stale in-progress")
    return removed


def _prune_dir(directory: str, max_age_seconds: float, label: str) -> int:
    if not os.path.isdir(directory):
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        try:
            # islink check keeps a symlink's target safe; only the link goes.
            if not os.path.islink(path) and os.path.isfile(path):
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
        except OSError:
            log.warning("Could not prune %s audio: %s", label, path)
    if removed:
        log.info("Pruned %d %s recording(s) from %s", removed, label, directory)
    return removed


def _touch_heartbeat(cfg: Config) -> None:
    """Record that the poll loop is alive, for the container healthcheck.

    `restart: unless-stopped` only reacts to the process exiting. A wedged
    Chromium or a hung poll leaves the container Up forever with nothing
    happening; a stale heartbeat is what makes that visible.
    """
    try:
        with open(cfg.heartbeat_file, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        log.debug("Could not write heartbeat file %s", cfg.heartbeat_file)


def main() -> None:
    # `notetaker download-model` runs before any other setting is read: the
    # wizard calls it after writing .env and before the container starts.
    if sys.argv[1:] == ["download-model"]:
        from notetaker.whisper import download_from_env

        sys.exit(download_from_env())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = Config.from_env()

    # Validate every dependency now, not at minute 79 of a real meeting.
    # Exiting non-zero makes the container crash-loop, which docker ps and the
    # weekly health probe both notice — unlike the previous silent failure.
    if os.getenv("SKIP_PREFLIGHT", "").lower() not in ("1", "true", "yes"):
        failures = run_preflight(cfg)
        if failures:
            log.error(
                "Refusing to start with %d failed preflight check(s). "
                "Set SKIP_PREFLIGHT=1 to override.",
                len(failures),
            )
            sys.exit(1)

    monitor = CallMonitor(cfg.nextcloud_url, cfg.nextcloud_user, cfg.nextcloud_password)

    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        log.info("Received signal %s, shutting down...", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("Notetaker started. Polling every %ds...", cfg.poll_interval)
    log.info("Email overrides: %s", cfg.email_overrides)
    if cfg.email_enabled:
        log.info("SMTP: %s:%d, from=%s", cfg.smtp_host, cfg.smtp_port, cfg.smtp_from)
    else:
        log.info("Email disabled: SMTP_HOST is unset, notes will only be stored")

    poll_tick = 0
    while not shutdown:
        _touch_heartbeat(cfg)
        try:
            # Retry anything that failed to send earlier. A transient mail
            # outage now self-heals without any human action.
            drain_spool(cfg)

            # Cheap and idempotent; keeps retained audio bounded.
            if poll_tick % 360 == 0:
                prune_retained_audio(cfg)
            poll_tick += 1

            new_calls = monitor.check_for_new_calls()
            if len(new_calls) > 1:
                log.warning(
                    "Multiple concurrent calls detected (%d). Processing sequentially; "
                    "later calls may be partially missed.",
                    len(new_calls),
                )
            for room in new_calls:
                log.info(
                    "New call detected: %s (%s)",
                    room.get("displayName"),
                    room["token"],
                )
                try:
                    asyncio.run(handle_call(cfg, room))
                except Exception:
                    log.exception(
                        "Failed to process call in room '%s' (%s)",
                        room.get("displayName"),
                        room["token"],
                    )
                finally:
                    # Clear from active set so if the call is still ongoing
                    # (e.g. we left early), it gets re-detected next poll
                    monitor.clear_token(room["token"])

            # Only when nothing live is waiting: a retry transcribes a whole
            # meeting on this thread, and a call that starts meanwhile would
            # be joined late. New calls always come first.
            if not new_calls:
                drain_retained(cfg)
        except Exception:
            log.exception("Error in poll cycle")

        time.sleep(cfg.poll_interval)

    log.info("Notetaker stopped.")


if __name__ == "__main__":
    main()
