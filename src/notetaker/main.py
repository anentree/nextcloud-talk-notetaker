from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime

from notetaker import spool
from notetaker.config import Config
from notetaker.mailer import extract_follow_up_email, send_notes_email
from notetaker.monitor import CallMonitor
from notetaker.participants import get_participant_emails
from notetaker.preflight import run_preflight
from notetaker.recorder import AudioRecorder
from notetaker.storage import upload_notes
from notetaker.transcriber import transcribe_and_summarize

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

    # Everything from here on must leave the recording in a defined state:
    # deleted if the notes are safe, moved somewhere persistent if not. An
    # exception escaping in between (a Gemini outage, a bad display name)
    # would otherwise strand the file in tmpfs until the next restart.
    notes_saved = False
    try:
        notes_saved = await _process_recording(
            cfg, token, name, call_start, audio_path, speaker_events, attendee_ids
        )
    finally:
        if notes_saved:
            _discard_audio(audio_path)
        elif os.path.exists(audio_path):
            _retain_audio(cfg, audio_path, token)


async def _process_recording(
    cfg: Config,
    token: str,
    name: str,
    call_start: datetime,
    audio_path: str,
    speaker_events: list,
    attendee_ids: set,
) -> bool:
    """Transcribe, store and email one recording. Returns True if the notes
    reached durable storage (which is what decides whether the audio can be
    thrown away)."""
    # 2. Check audio is not empty
    audio_size = os.path.getsize(audio_path)
    if audio_size == 0:
        log.warning("Empty audio file for room '%s' — skipping transcription", name)
        # Nothing to preserve: an empty file is not evidence of anything.
        return True
    log.info("Audio file: %s (%d KB)", audio_path, audio_size // 1024)

    # 3. Get participants (needed for filename and email)
    # Only people actually observed in the call. A room can have members who
    # never joined; they must not receive the notes.
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

    # 4. Transcribe + summarize
    # Chunked transcription of a long call takes many minutes; keep the
    # heartbeat fresh so the container doesn't report unhealthy while it is
    # doing exactly what it is supposed to do.
    _touch_heartbeat(cfg)
    participant_names = [p["display_name"] for p in participants]
    notes = transcribe_and_summarize(
        cfg.gemini_api_key,
        audio_path,
        name,
        model=cfg.gemini_model,
        speaker_events=speaker_events,
        participant_names=participant_names,
        heartbeat_fn=lambda: _touch_heartbeat(cfg),
    )
    _touch_heartbeat(cfg)

    # 5. Save notes
    filename = _build_notes_filename(
        participants, call_start, last_user=cfg.filename_last_user
    )
    notes_saved = False
    try:
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
        notes_saved = True
    except Exception:
        # Keep going: the email below is now the ONLY copy of these notes,
        # which makes delivering it more important, not less.
        log.exception("Failed to save notes for room %s", token)

    # 6. Email participants (retried, then spooled — never dropped)
    if participants:
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
    else:
        log.warning("No participants with email found for room %s", token)
        delivered = False

    log.info(
        "Done processing call in room '%s' (notes_saved=%s, email_delivered=%s)",
        name,
        notes_saved,
        delivered,
    )
    # The caller decides what to do with the audio based on this. Deleting the
    # recording at the exact moment the pipeline is known to be broken is how
    # the 2026-08-13 calls became unrecoverable.
    return notes_saved


def _discard_audio(audio_path: str) -> None:
    try:
        os.unlink(audio_path)
        log.info("Cleaned up audio file: %s", audio_path)
    except OSError:
        log.warning("Failed to clean up audio file: %s", audio_path)


def _retain_audio(cfg: Config, audio_path: str, token: str) -> None:
    """Move the recording somewhere persistent so the call can be reprocessed.

    AUDIO_DIR is a tmpfs mount, so leaving the file in place would lose it on
    the next container restart.
    """
    try:
        os.makedirs(cfg.retained_audio_dir, exist_ok=True)
        dest = os.path.join(
            cfg.retained_audio_dir, f"{token}-{os.path.basename(audio_path)}"
        )
        shutil.move(audio_path, dest)
        log.error(
            "Notes could not be saved for room %s — recording retained at %s "
            "for manual reprocessing",
            token,
            dest,
        )
    except Exception:
        log.exception(
            "Failed to retain audio for room %s; leaving it at %s", token, audio_path
        )


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
    log.info("SMTP: %s:%d, from=%s", cfg.smtp_host, cfg.smtp_port, cfg.smtp_from)

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
        except Exception:
            log.exception("Error in poll cycle")

        time.sleep(cfg.poll_interval)

    log.info("Notetaker stopped.")


if __name__ == "__main__":
    main()
