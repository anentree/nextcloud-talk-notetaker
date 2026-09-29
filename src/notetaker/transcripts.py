"""The word-for-word transcript of a call, kept beside the notes.

The notes are a summary and get emailed to everyone who attended. The
transcript is the exact record and goes to one place only: TRANSCRIPTS_FOLDER
(or LOCAL_TRANSCRIPTS_DIR). It is never attached to or quoted in an email.

Only the Whisper path produces a verbatim transcript. The Gemini audio path
writes notes straight from the audio, so a call that went that way has no
transcript to keep.
"""

from __future__ import annotations

import logging
import os

from notetaker.storage import upload_notes

log = logging.getLogger(__name__)


def build_document(
    transcript: str,
    conversation_name: str,
    call_start: str,
    attendee_names: list[str] | None = None,
) -> str:
    """Markdown for the transcript file.

    The transcript's lines ("[MM:SS] Name: words") are kept exactly, one
    blank line apart: Markdown joins consecutive lines into one paragraph,
    which would render an hour of conversation as a single wall of text.
    """
    header = [f"# Transcript: {conversation_name}", "", f"**Date:** {call_start}"]
    if attendee_names:
        header.append(f"**Attendees:** {', '.join(attendee_names)}")
    header += [
        "",
        (
            "> Verbatim machine transcript (Whisper), unedited. Private: "
            "it is not emailed or shared with the participants."
        ),
        "",
    ]
    lines = [line for line in transcript.splitlines() if line.strip()]
    return "\n".join(header) + "\n" + "\n\n".join(lines) + "\n"


def save(cfg, filename: str, document: str) -> str:
    """Write the transcript to its private destination. Returns where it went.
    Raises on failure; the caller decides whether to retry.

    Re-checks the destination every time: startup validation can be skipped
    (SKIP_PREFLIGHT) and reprocess.py never runs it, and a transcript written
    into the shared notes folder cannot be taken back."""
    problems = cfg.transcripts_problems()
    if problems:
        raise RuntimeError("; ".join(problems))
    if cfg.notes_storage == "local":
        os.makedirs(cfg.local_transcripts_dir, exist_ok=True)
        path = os.path.join(cfg.local_transcripts_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(document)
        log.info("Saved transcript locally to %s", path)
        return path
    url = upload_notes(
        cfg.nextcloud_url,
        cfg.nextcloud_user,
        cfg.nextcloud_password,
        cfg.transcripts_folder,
        filename,
        document,
    )
    log.info("Saved transcript to %s/%s", cfg.transcripts_folder, filename)
    return url
