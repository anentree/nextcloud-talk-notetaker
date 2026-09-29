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

from . import retained, transcriber, transcripts, verify
from .config import Config
from .storage import upload_notes
from .transcriber import NO_SPEECH_SENTINEL, make_notes

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


def release(cfg: Config, audio_path: str) -> int:
    """Finish a held recording as if it had passed verification: the same
    pipeline the live call would have run, with the notes it already made."""
    from .main import _retry_recording

    path = retained.sidecar_path(audio_path)
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError) as exc:
        log.error("No readable sidecar at %s: %s", path, exc)
        return 1
    if not meta.get("held"):
        log.error(
            "%s was not held by content verification; reprocess it without "
            "--release instead",
            audio_path,
        )
        return 1
    log.info("Releasing %s, held because: %s", audio_path, "; ".join(meta["held"]))
    meta.pop("parked", None)
    meta["content_accepted"] = True
    try:
        ok = _retry_recording(cfg, meta, audio_path)
    finally:
        # Whatever happened, keep what was learned (notes_saved, email_done)
        # so running this again cannot email the attendees twice.
        retained._atomic_write(path, meta)
    if not ok:
        log.error(
            "Released, but the notes or transcript could not be saved. The "
            "recording is kept and the notetaker will retry it on its own; do "
            "not run --release again"
        )
        return 1
    for leftover in (audio_path, path):
        try:
            os.unlink(leftover)
        except OSError:
            log.warning("Could not remove %s", leftover)
    log.info("Released and delivered; the recording is deleted")
    return 0


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
    parser.add_argument(
        "--force",
        action="store_true",
        help="upload the notes even if they fail content verification",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help=(
            "a recording HELD by content verification: accept its output as it "
            "is, then save the notes, email the attendees and delete the audio"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if not os.path.exists(args.audio_path):
        log.error("No such recording: %s", args.audio_path)
        return 1

    cfg = Config.from_env()
    if args.release:
        return release(cfg, args.audio_path)
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
    if meta.get("held") and not (args.dry_run or args.out):
        # Uploading here would publish notes that failed verification into
        # the shared folder. Only --release is a decision to do that.
        log.error(
            "%s is HELD by content verification (%s). Use --release to publish "
            "it anyway, or --dry-run/--out to look at it",
            args.audio_path,
            "; ".join(meta["held"]),
        )
        return 1
    notes = make_notes(
        cfg,
        meta,
        args.audio_path,
        name,
        call_date or None,
        participant_names=participants or None,
    )
    log.info("Produced %d chars of notes", len(notes))

    # Same check as a live call before anything reaches the shared folder: a
    # recording retained during an outage has never been judged.
    try:
        duration_s = transcriber._get_audio_duration(args.audio_path)
    except Exception:  # noqa: BLE001 — unknown length: verify.py holds
        duration_s = meta.get("recorded_s")
    problems = verify.check_content(
        cfg,
        notes,
        meta.get("transcript")
        if meta.get("transcript") is not None
        else meta.get("gemini_transcript"),
        duration_s,
        NO_SPEECH_SENTINEL,
    )
    if problems and not (args.dry_run or args.out or args.force):
        log.error(
            "Not uploading: the notes failed content verification (%s). Look "
            "with --dry-run or --out, or upload anyway with --force",
            "; ".join(problems),
        )
        return 1

    if args.dry_run:
        sys.stdout.write(notes)
        return 0

    transcript = meta.get("transcript")
    if transcript == NO_SPEECH_SENTINEL:
        transcript = None
    document = (
        transcripts.build_document(
            transcript, name, call_date or "undated", participants or None
        )
        if transcript
        else None
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(notes)
        log.info("Wrote notes to %s", args.out)
        if document and cfg.transcripts_enabled:
            stem, ext = os.path.splitext(args.out)
            out = f"{stem}-transcript{ext or '.md'}"
            with open(out, "w", encoding="utf-8") as f:
                f.write(document)
            log.info("Wrote the transcript to %s", out)
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
    if document and cfg.transcripts_enabled:
        transcripts.save(cfg, filename, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
