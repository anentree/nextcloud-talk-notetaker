"""Replay a retained recording through the transcription pipeline.

When the pipeline fails, main.py moves the recording into
`retained-audio/` and logs "retained ... for manual reprocessing" — but
until this module there was nothing to reprocess it *with*, so the retained
file was a promise the code could not keep. This is that tool.

Usage inside the container:

    python -m notetaker.reprocess /data/notes/retained-audio/<file>.webm
    python -m notetaker.reprocess <file>.webm --participants Jo,brantley
    python -m notetaker.reprocess <file>.webm --dry-run

Since 2026-09-14 a retained recording has a `<file>.webm.json` sidecar beside
it holding the speaker timeline and call context; when it exists it is used.
Older recordings have only the mixed audio. Pass `--participants` with the
attendees that were ACTUALLY observed in the call (they are in the container
log, "Call attendees observed in-call"). Do not pass the room's member list —
supplying names that were never present is what made Gemini invent speakers
and hand them action items.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

from . import retained
from .config import Config
from .storage import upload_notes
from .transcriber import make_notes

log = logging.getLogger("notetaker.reprocess")

# Retained files are named "<token>-<YYYY-MM-DD>-<room-name-slug>.webm".
RETAINED_RE = re.compile(
    r"^(?P<token>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>.+)$"
)


def parse_retained_name(path: str) -> tuple[str, str, str]:
    """Return (token, date, conversation_name) parsed from a retained filename."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = RETAINED_RE.match(stem)
    if not m:
        return "", "", stem.replace("-", " ").title()
    return (
        m.group("token"),
        m.group("date"),
        m.group("slug").replace("-", " ").title(),
    )


def _load_sidecar(audio_path: str) -> dict:
    """The retained sidecar written beside the recording, or {} for a
    recording that predates it."""
    path = retained.sidecar_path(audio_path)
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    log.info("Using the speaker timeline and call context from %s", path)
    if meta.pop("parked", False):
        # Parked after repeated Whisper failures; running this by hand is
        # the decision to give Whisper a fresh set of tries.
        meta.pop("whisper_failures", None)
        meta.pop("attempts", None)
        log.info("Recording was parked; retrying Whisper from scratch")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_path", help="path to the retained .webm recording")
    parser.add_argument(
        "--participants",
        default="",
        help="comma-separated attendees ACTUALLY observed in the call",
    )
    parser.add_argument(
        "--name", default="", help="override the conversation name for the notes header"
    )
    parser.add_argument(
        "--out",
        default="",
        help="write notes to this local file instead of uploading to Nextcloud",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="transcribe and print to stdout without saving anywhere",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if not os.path.exists(args.audio_path):
        log.error("No such recording: %s", args.audio_path)
        return 1

    cfg = Config.from_env()
    token, call_date, parsed_name = parse_retained_name(args.audio_path)
    name = args.name or parsed_name
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]

    size_kb = os.path.getsize(args.audio_path) / 1024
    log.info(
        "Reprocessing %s (%.0f KB) as '%s'%s",
        args.audio_path,
        size_kb,
        name,
        f" with attendees {participants}" if participants else " with no attendee list",
    )

    meta = _load_sidecar(args.audio_path)
    notes = make_notes(
        cfg,
        meta,
        args.audio_path,
        name,
        call_date or None,
        participant_names=participants or None,
    )
    log.info("Produced %d chars of notes", len(notes))

    if args.dry_run:
        sys.stdout.write(notes)
        return 0

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(notes)
        log.info("Wrote notes to %s", args.out)
        return 0

    filename = f"{call_date or 'undated'}-{token or 'recovered'}-recovered.md"
    upload_notes(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        cfg.notes_folder,
        filename,
        notes,
    )
    log.info("Uploaded recovered notes as %s/%s", cfg.notes_folder, filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
