"""Decide whether a finished call's output is real before the audio goes.

Saving the notes used to be the whole test: once the file was uploaded, the
recording was deleted. On 2026-09-29 the bot was dropped from a 78-minute
call and recorded silence; Whisper found 24 characters of speech, the notes
step wrote a tidy summary of nothing, it was saved, emailed to an outside
attendee, and the only evidence was deleted. A saved file is not a result.
What is checked here is the content.
"""

from __future__ import annotations

import re

# A summary shorter than this is a header and nothing else.
MIN_NOTES_CHARS = 150

_TIMESTAMP = re.compile(r"^\[\d{1,3}:\d{2}\]\s*")
# Lines that are the pipeline talking, not the call: Gemini's markers for a
# silent or failed segment.
_MARKER = re.compile(r"^\[(NO SPEECH|SEGMENT \d+ OF \d+ UNAVAILABLE)", re.IGNORECASE)


def speech_chars(transcript: str) -> int:
    """Characters of transcribed speech: timestamps and pipeline markers
    removed, and each distinct line counted once. Whisper hallucinates on
    silence ("Thanks for watching!" over and over); repeating a line must
    not add up to a conversation."""
    seen: set[str] = set()
    total = 0
    for raw in transcript.splitlines():
        line = _TIMESTAMP.sub("", raw).strip()
        if not line or _MARKER.match(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        total += len(line)
    return total


def check_content(
    cfg,
    notes: str,
    transcript: str | None,
    duration_s: float | None,
    no_speech_marker: str,
) -> list[str]:
    """Reasons the call's output cannot be trusted, or [] if it can.

    ``transcript`` is the Whisper transcript, or the Gemini segment
    transcripts; None when there is neither (Gemini single-pass). Every
    unknown fails closed: holding a good call costs an alert, publishing a
    bad one costs the call.
    """
    if not duration_s:
        # Fail closed: without the length there is no judging the content.
        return ["the length of the recording could not be read"]
    minutes = duration_s / 60
    short = minutes < cfg.verify_min_minutes
    if short and transcript == no_speech_marker:
        # Someone opened a call and closed it. The one-line "no speech"
        # notes are the right output, not a failure worth an alert.
        return []

    problems: list[str] = []
    notes_chars = len((notes or "").strip())
    if notes_chars < MIN_NOTES_CHARS:
        problems.append(f"the notes are empty ({notes_chars} characters)")
    if short:
        # A short call can legitimately be almost silent.
        return problems
    if transcript is None:
        # Notes written straight from audio, with nothing to check them
        # against. Fail closed rather than trust them.
        problems.append(
            f"there is no transcript to check the notes of a {minutes:.0f}-minute "
            "call against"
        )
        return problems

    chars = 0 if transcript == no_speech_marker else speech_chars(transcript)
    per_minute = chars / minutes
    if per_minute < cfg.min_speech_chars_per_minute:
        problems.append(
            f"only {chars} characters of speech in a {minutes:.0f}-minute "
            f"recording ({per_minute:.1f} per minute; a real conversation is "
            f"several hundred, the minimum is {cfg.min_speech_chars_per_minute})"
        )
    return problems
