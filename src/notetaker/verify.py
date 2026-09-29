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

_TIMESTAMP = re.compile(r"^\[\d{1,3}:\d{2}\]\s*", re.MULTILINE)


def speech_chars(transcript: str) -> int:
    """Characters of transcribed speech, without the [MM:SS] stamps."""
    return len(_TIMESTAMP.sub("", transcript).strip())


def check_content(
    cfg,
    notes: str,
    transcript: str | None,
    duration_s: float | None,
    no_speech_marker: str,
) -> list[str]:
    """Reasons the call's output cannot be trusted, or [] if it can.

    ``transcript`` is None when the notes came straight from audio (the
    Gemini path), which leaves only the notes to judge. ``duration_s`` is
    None when the recording's length could not be read; the speech check is
    then skipped rather than guessed at.
    """
    short = bool(duration_s) and duration_s / 60 < cfg.verify_min_minutes
    if short and transcript == no_speech_marker:
        # Someone opened a call and closed it. The one-line "no speech"
        # notes are the right output, not a failure worth an alert.
        return []

    problems: list[str] = []
    notes_chars = len((notes or "").strip())
    if notes_chars < MIN_NOTES_CHARS:
        problems.append(f"the notes are empty ({notes_chars} characters)")

    if transcript is None or not duration_s or short:
        # No transcript (Gemini audio path) or no length: only the notes can
        # be judged. A short call can legitimately be almost silent.
        return problems

    minutes = duration_s / 60
    chars = 0 if transcript == no_speech_marker else speech_chars(transcript)
    per_minute = chars / minutes
    if per_minute < cfg.min_speech_chars_per_minute:
        problems.append(
            f"only {chars} characters of speech in a {minutes:.0f}-minute "
            f"recording ({per_minute:.1f} per minute; a real conversation is "
            f"several hundred, the minimum is {cfg.min_speech_chars_per_minute})"
        )
    return problems
