from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from datetime import datetime

from notetaker.config import Config
from notetaker.mailer import extract_follow_up_email, send_notes_email
from notetaker.monitor import CallMonitor
from notetaker.participants import get_participant_emails
from notetaker.recorder import AudioRecorder
from notetaker.storage import upload_notes
from notetaker.transcriber import transcribe_and_summarize

log = logging.getLogger(__name__)


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
    audio_path = await recorder.record_call(token, name)

    # 2. Check audio is not empty
    audio_size = os.path.getsize(audio_path)
    if audio_size == 0:
        log.warning("Empty audio file for room '%s' — skipping transcription", name)
        try:
            os.unlink(audio_path)
        except OSError:
            pass
        return
    log.info("Audio file: %s (%d KB)", audio_path, audio_size // 1024)

    # 3. Get participants (needed for filename and email)
    participants = get_participant_emails(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        token,
        mail_domain=cfg.mail_domain,
        email_overrides=cfg.email_overrides,
        exclude_user=cfg.nextcloud_user,
    )

    # 4. Transcribe + summarize
    notes = transcribe_and_summarize(
        cfg.gemini_api_key, audio_path, name, model=cfg.gemini_model
    )

    # 5. Save notes
    filename = _build_notes_filename(
        participants, call_start, last_user=cfg.filename_last_user
    )
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

    # 6. Email participants
    try:
        if participants:
            subject, body = extract_follow_up_email(notes)
            send_notes_email(
                smtp_host=cfg.smtp_host,
                smtp_port=cfg.smtp_port,
                smtp_from=cfg.smtp_from or f"{cfg.nextcloud_user}@localhost",
                smtp_user=cfg.smtp_user,
                smtp_password=cfg.smtp_password,
                recipients=participants,
                subject=subject,
                body=body,
                notes_markdown=notes,
                attachment_filename=filename,
            )
        else:
            log.warning("No participants with email found for room %s", token)
    except Exception:
        log.exception("Failed to send email for room %s (notes still uploaded)", token)

    # 7. Clean up audio file
    try:
        os.unlink(audio_path)
        log.info("Cleaned up audio file: %s", audio_path)
    except OSError:
        log.warning("Failed to clean up audio file: %s", audio_path)
    log.info("Done processing call in room '%s'", name)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = Config.from_env()
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

    while not shutdown:
        try:
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
