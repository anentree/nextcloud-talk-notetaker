from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from datetime import date

from notetaker.config import Config
from notetaker.mailer import extract_follow_up_email, send_notes_email
from notetaker.monitor import CallMonitor
from notetaker.participants import get_participant_emails
from notetaker.recorder import AudioRecorder
from notetaker.storage import upload_notes
from notetaker.transcriber import transcribe_and_summarize

log = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


async def handle_call(cfg: Config, room: dict) -> None:
    token = room["token"]
    name = room.get("displayName", token)
    log.info("Processing call in room '%s' (%s)", name, token)

    # 1. Record audio
    recorder = AudioRecorder(
        cfg.nextcloud_url, cfg.nextcloud_user, cfg.nextcloud_password, cfg.audio_dir
    )
    audio_path = await recorder.record_call(token, name)

    # 2. Transcribe + summarize
    notes = transcribe_and_summarize(cfg.gemini_api_key, audio_path, name)

    # 3. Upload to Nextcloud
    slug = _slugify(name) or token
    filename = f"{date.today().isoformat()}-{slug}.md"
    upload_notes(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        cfg.notes_folder,
        filename,
        notes,
    )

    # 4. Email participants
    participants = get_participant_emails(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        token,
        exclude_user=cfg.nextcloud_user,
    )
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
        )
    else:
        log.warning("No participants with email found for room %s", token)

    # 5. Clean up audio file
    if os.path.exists(audio_path):
        os.remove(audio_path)
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

    while not shutdown:
        try:
            new_calls = monitor.check_for_new_calls()
            for room in new_calls:
                log.info(
                    "New call detected: %s (%s)",
                    room.get("displayName"),
                    room["token"],
                )
                asyncio.run(handle_call(cfg, room))
        except Exception:
            log.exception("Error in poll cycle")

        time.sleep(cfg.poll_interval)

    log.info("Notetaker stopped.")


if __name__ == "__main__":
    main()
