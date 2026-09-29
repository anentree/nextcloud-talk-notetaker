from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
import time
from datetime import date

from google import genai
from google.genai import types

from notetaker.attribution import label_transcript
from notetaker.claude_notes import claude_call
from notetaker.whisper import WhisperFailed
from notetaker.whisper import transcribe as whisper_transcribe

log = logging.getLogger(__name__)

# Retry budget for one Gemini call. On 2026-09-08 the API answered 503 "high
# demand" on and off for about two and a half minutes; three attempts 5s and
# 10s apart is a 15-second window and lost the meeting. Eight attempts with
# doubling, capped delays wait 5+10+20+40+80+120+120 = 395s, which is longer
# than any spike seen so far. Anything longer is handled by retained.py.
MAX_RETRIES = 8
RETRY_BASE_DELAY = 5  # seconds
RETRY_MAX_DELAY = 120  # seconds
# An empty response (MAX_TOKENS, SAFETY, RECITATION) is mostly deterministic
# on the same input, and every attempt re-uploads the whole segment. One
# retry covers the odd flake; the full transient budget would cost 395s and
# eight uploads to learn nothing new.
EMPTY_RESPONSE_ATTEMPTS = 2
# The container healthcheck declares the process dead after roughly 170s
# without a heartbeat, so a long backoff sleep must be sliced with beats.
HEARTBEAT_SLICE = 30  # seconds
CHUNK_DURATION = 900  # 15 minutes per chunk
MIN_CHARS_PER_MINUTE = 100  # quality gate: minimum transcript density
FALLBACK_MODEL = "gemini-2.5-flash"  # stronger model for retries

# A 15-minute segment of dense conversation transcribes to more than the 8192
# default allowed. On 2026-08-25 successive segments returned 14887, 17529 and
# 19350 chars — climbing toward the cap — and the next one came back with no
# text part at all. Give a segment the same budget the synthesis step gets.
SEGMENT_MAX_OUTPUT_TOKENS = 16384

# Whisper failures on one recording before that recording goes to Gemini
# audio instead. Two clean failures (the live attempt and the first retry)
# or, via retained.drain()'s pre-written `attempts`, three attempts that
# died without recording anything (attempts > WHISPER_MAX_FAILURES). Assumes nothing before make_notes raises on
# the retry path, which is true today (get_participant_emails returns []).
WHISPER_MAX_FAILURES = 2
# NOTES_BACKEND=claude has no audio fallback, so Whisper gets two more tries
# instead. Past this the recording is parked: a recording that fails every
# time (corrupt audio, or one that OOM-kills the container) would otherwise
# be retried for 30 days, each run blocking the poll loop for up to ~45 min.
CLAUDE_WHISPER_MAX_FAILURES = 4
# Stored in meta["transcript"] when Whisper finds no speech, so a retry
# never re-runs a 45-minute transcription to learn the same thing.
NO_SPEECH_SENTINEL = "[NO SPEECH DETECTED]"


class EmptyGeminiResponse(RuntimeError):
    """Gemini returned HTTP 200 but the candidate carried no text part.

    `GenerateContentResponse.text` is typed Optional[str]: None is part of the
    success contract, not an error path, so nothing is raised for the retry
    loop to catch. It happens when the token budget is spent before any text
    is emitted (finish_reason MAX_TOKENS), or when the candidate is withheld
    (SAFETY, RECITATION). Turning it into an exception is what lets the
    existing retry-and-fallback machinery see it.
    """


class UpstreamUnavailable(RuntimeError):
    """Gemini stayed unavailable for the whole transient retry budget.

    Distinct from a per-segment failure on purpose: the chunk loop swallows
    a segment that cannot be transcribed so the rest of the meeting survives,
    but an outage is not a property of the segment. Swallowing it produced
    notes with a hole in them, after which the caller deleted the recording
    - and on the retry path an outage ending mid-retry turned a fully
    retained recording into permanently incomplete notes. This propagates
    so the whole recording is retained intact and tried again later.
    """


SEGMENT_PROMPT = """You are a professional meeting transcriber. This audio is segment {seg_num} of {total_segs} from a Nextcloud Talk call named "{conversation_name}" on {date}.

This segment covers approximately minutes {start_min} to {end_min} of the call.
{participants_block}{timeline_block}
CRITICAL RULES:
- Transcribe EVERYTHING said in this segment. Be thorough — do not skip or summarize.
- Only include words that were ACTUALLY SPOKEN. Do NOT invent or hallucinate dialogue.
- If the audio is silent or unintelligible, respond with: "[NO SPEECH IN THIS SEGMENT]"
- If a SPEAKER TIMELINE is provided above, use it as the primary guide for speaker attribution — it was derived from separate per-participant audio tracks and is highly reliable. Attribute utterances to the speaker whose interval contains the timestamp.
- If the timeline labels are generic (e.g. "Speaker 1") or absent, separate speakers by voice and KEEP the generic labels ("Speaker 1", "Speaker 2"). Only use a real name when someone is addressed by name in the audio or introduces themselves. NEVER assign a name from the participant list just because it is on the list — an unnamed speaker must stay unnamed.
- The number of distinct speakers is whatever you can actually hear. Do NOT try to match the length of the participant list. A two-person call with four people on the roster has TWO speakers.
- If an interval is marked `[overlap]`, note crosstalk between the listed speakers.
- The call took place on {date}; use that date for any date references.

Produce a detailed transcript of this segment in this format:

## Segment {seg_num} (minutes {start_min}-{end_min})

**Topics covered:** [Brief list of topics in this segment]

**Transcript:**
[Detailed chronological transcript with speaker attribution. Include all discussion points, decisions, action items, and notable statements. Use direct quotes for important statements.]
"""

NOTES_FORMAT_BLOCK = """Produce the notes in this markdown format:

# Meeting Notes: {conversation_name}
**Date:** {date}
**Duration:** ~{duration_min} minutes
**Participants:** [Names from transcripts]

---

## Topics Discussed

### 1. [First Topic Title]
[Detailed summary of discussion, decisions, and outcomes. Include who said what for important points. Use sub-bullets for specifics.]

### 2. [Second Topic Title]
[Continue for each major topic...]

[Continue numbering for ALL topics covered in the call]

---

## Action Items
| # | Owner | Action | Deadline |
|---|-------|--------|----------|
| 1 | [Name] | [What needs to be done] | [When, if mentioned] |

## Decisions Made
- [Decision 1 — context/rationale if discussed]
- [Decision 2]

## Open Questions / Follow-Ups
- [Any unresolved items or things to follow up on]

---

## Follow-Up Email

Subject: Meeting Notes — {conversation_name} — {date}

Hi everyone,

Here's a summary of our call on {date} ({duration_min} minutes):

[3-5 sentence overview of the main topics and outcomes]

**Key decisions:**
[Bullet list of decisions]

**Action items:**
[Bullet list with owners]

Please let me know if I missed anything.

Best,
AI Notetaker

---

Be thorough — this is the official record of the meeting. Every topic discussed should appear in the notes.
"""

SYNTHESIS_PROMPT = (
    """You are a professional meeting note-taker. Below are detailed transcripts from all segments of a {duration_min}-minute Nextcloud Talk call named "{conversation_name}" on {date}.
{participants_block}{timeline_block}
Your job is to synthesize these segment transcripts into comprehensive, well-structured meeting notes.

CRITICAL RULES:
- Use ONLY information from the transcripts below. Do NOT invent or add anything not present.
- Cover the ENTIRE call chronologically — every topic, decision, and action item.
- Use speaker names from the SPEAKER TIMELINE. If the timeline has generic labels (Speaker 1, etc.), keep those generic labels unless the transcripts show the person being addressed by name.
- NEVER attribute a statement or an action item to someone who does not appear in the segment transcripts. Attributing words to a person who was not on the call is the single worst error you can make here — it puts commitments in the mouths of people who never spoke.
- The Participants line and every attribution must list ONLY people evidenced in the transcripts. The roster is not evidence. If only two people spoke, name two people, even if the roster lists more.
- If you cannot tell who said something, write "Unattributed" rather than guessing a name.
- If the transcripts mention specific numbers, dates, names, or amounts — use them exactly.

"""
    + NOTES_FORMAT_BLOCK
    + """
=== SEGMENT TRANSCRIPTS ===

{segments}
"""
)

TRANSCRIPT_SYNTHESIS_PROMPT = (
    """You are a professional meeting note-taker. Below is a transcript of a {duration_min}-minute Nextcloud Talk call named "{conversation_name}" on {date}, produced by speech recognition.
{participants_block}
Each transcript line is "[MM:SS] Speaker: words". The speaker labels come from the call's separate audio tracks and are authoritative: the name on a line is the person who said those words. "Unattributed" means the speaker is unknown; "A + B" means two people were talking at once. Lines with no speaker label mean no attribution was possible for this call.

CRITICAL RULES:
- Use ONLY information from the transcript below. Do NOT invent or add anything not present.
- Cover the ENTIRE call chronologically — every topic, decision, and action item.
- NEVER attribute a statement or an action item to someone who does not appear as a speaker label in the transcript. The roster is not evidence of attendance or of speech.
- If the speaker of a statement is "Unattributed" or missing, write "Unattributed" rather than guessing a name.
- Speech recognition mishears names. If a misheard name clearly matches a spelling in the POSSIBLE PARTICIPANTS list, use that spelling; introduce nothing else from that list.
- If the transcript mentions specific numbers, dates, names, or amounts — use them exactly.

"""
    + NOTES_FORMAT_BLOCK
    + """
=== TRANSCRIPT ===

{transcript}
"""
)


def _format_participants_block(names: list[str] | None) -> str:
    """Format participant names into a block for the prompt."""
    if not names:
        return ""
    return (
        "\nPOSSIBLE PARTICIPANTS (room roster — NOT proof of attendance).\n"
        "Some of these people may not have joined this call at all, and some "
        "may have joined without speaking. Treat this only as a spelling guide "
        "for names you can already hear. Never introduce a name from this list "
        "unless the audio or timeline supports it:\n" + ", ".join(names) + "\n"
    )


def _fmt_ts(ms: int) -> str:
    """Format milliseconds as MM:SS.s."""
    total_s = ms / 1000.0
    mm = int(total_s // 60)
    ss = total_s - mm * 60
    return f"{mm:02d}:{ss:04.1f}"


def _format_timeline_block(
    events: list[dict] | None,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> str:
    """Format speaker events into a compact text block for the prompt.

    If start_sec/end_sec are provided, only include events overlapping that window
    (timestamps are re-based to 0 = start_sec).

    Overlapping intervals from different speakers are merged into `[overlap: A+B]`
    lines so Gemini can mark crosstalk.
    """
    if not events:
        return ""

    # Window filter
    if start_sec is not None and end_sec is not None:
        w_start = int(start_sec * 1000)
        w_end = int(end_sec * 1000)
        windowed = []
        for e in events:
            s = max(e["start_ms"], w_start)
            en = min(e["end_ms"], w_end)
            if en > s:
                windowed.append(
                    {
                        "start_ms": s - w_start,
                        "end_ms": en - w_start,
                        "label": e["label"],
                    }
                )
        events = windowed
    if not events:
        return ""

    # Build sweep events for overlap detection
    evs = sorted(events, key=lambda e: (e["start_ms"], e["end_ms"]))
    # Collect unique speakers in order of first appearance
    speakers: list[str] = []
    for e in evs:
        if e["label"] not in speakers:
            speakers.append(e["label"])

    # Simple sweep: compute timeline segments where the set of active speakers
    # changes, emit one line per segment.
    points = set()
    for e in evs:
        points.add(e["start_ms"])
        points.add(e["end_ms"])
    pts = sorted(points)

    lines: list[str] = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if b <= a:
            continue
        active = [e["label"] for e in evs if e["start_ms"] <= a and e["end_ms"] >= b]
        if not active:
            continue
        if len(active) == 1:
            label = active[0]
        else:
            uniq = []
            for x in active:
                if x not in uniq:
                    uniq.append(x)
            label = uniq[0] if len(uniq) == 1 else "[overlap: " + "+".join(uniq) + "]"
        # Merge with previous line if same label and contiguous
        if lines:
            prev = lines[-1]
            if prev["label"] == label and prev["end_ms"] == a:
                prev["end_ms"] = b
                continue
        lines.append({"start_ms": a, "end_ms": b, "label": label})

    if not lines:
        return ""

    header = (
        "\nSPEAKER TIMELINE (voice-activity ground truth — attribute utterances by time):\n"
        f"Participants: {', '.join(speakers)}\n"
    )
    body = "\n".join(
        f"[{_fmt_ts(l['start_ms'])}-{_fmt_ts(l['end_ms'])}] {l['label']}" for l in lines
    )
    return header + body + "\n"


def _get_audio_duration(audio_path: str) -> float:
    """Get audio duration in seconds using ffprobe packet timestamps."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            audio_path,
        ],
        capture_output=True,
        text=True,
    )
    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    if not lines:
        # Fallback: try format duration
        result2 = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                audio_path,
            ],
            capture_output=True,
            text=True,
        )
        val = result2.stdout.strip()
        if val and val != "N/A":
            return float(val)
        raise RuntimeError(f"Cannot determine duration of {audio_path}")
    return float(lines[-1])


def _split_to_chunks(
    audio_path: str, duration: float, chunk_dir: str
) -> list[tuple[str, float, float]]:
    """Split audio into MP3 chunks of CHUNK_DURATION seconds. Returns list of (path, start_sec, end_sec)."""
    chunks = []
    num_chunks = math.ceil(duration / CHUNK_DURATION)
    for i in range(num_chunks):
        start = i * CHUNK_DURATION
        end = min(start + CHUNK_DURATION, duration)
        chunk_path = os.path.join(chunk_dir, f"chunk_{i:02d}.mp3")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                audio_path,
                "-ss",
                str(start),
                "-t",
                str(CHUNK_DURATION),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                chunk_path,
            ],
            capture_output=True,
        )
        size = os.path.getsize(chunk_path)
        log.info("Chunk %d: %.0f-%.0f min, %d KB", i, start / 60, end / 60, size / 1024)
        chunks.append((chunk_path, start, end))
    return chunks


def _finish_reason(response) -> str:
    """Best-effort finish_reason for diagnostics; never raises."""
    try:
        candidate = (response.candidates or [None])[0]
        reason = getattr(candidate, "finish_reason", None)
        return str(getattr(reason, "name", reason) or "UNKNOWN")
    except Exception:  # pragma: no cover - diagnostics must not mask the error
        return "UNKNOWN"


def _sleep_with_heartbeat(delay: float, heartbeat_fn=None) -> None:
    """Sleep ``delay`` seconds in slices, proving liveness between them."""
    remaining = delay
    while remaining > 0:
        step = min(remaining, HEARTBEAT_SLICE)
        time.sleep(step)
        remaining -= step
        if heartbeat_fn is not None:
            try:
                heartbeat_fn()
            except Exception:
                log.debug("Heartbeat callback failed", exc_info=True)


TRANSIENT_MARKERS = (
    "429",
    "resource_exhausted",
    "503",
    "500",
    "unavailable",
    "deadline",
    "timeout",
    "connection",
    "reset",
    "broken pipe",
)


def _gemini_call(
    client: genai.Client,
    contents: list,
    max_output_tokens: int = 8192,
    model: str = "gemini-2.5-flash-lite",
    heartbeat_fn=None,
) -> str:
    """Make a Gemini API call with retry logic for all transient errors.

    Raises UpstreamUnavailable once a transient error has used up
    MAX_RETRIES, EmptyGeminiResponse after EMPTY_RESPONSE_ATTEMPTS empty
    answers, and anything else (a bad key, a malformed request) at once.
    """
    attempt = 0  # transient errors
    empty = 0  # HTTP 200 with no text part
    while True:
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
            )
            text = response.text
            if text is None:
                raise EmptyGeminiResponse(
                    "Gemini returned no text part "
                    f"(finish_reason={_finish_reason(response)}, model={model}, "
                    f"max_output_tokens={max_output_tokens})"
                )
            return text
        except EmptyGeminiResponse as exc:
            empty += 1
            if empty >= EMPTY_RESPONSE_ATTEMPTS:
                raise
            log.warning(
                "Gemini empty response (attempt %d/%d): %s. Retrying in %ds...",
                empty,
                EMPTY_RESPONSE_ATTEMPTS,
                exc,
                RETRY_BASE_DELAY,
            )
            _sleep_with_heartbeat(RETRY_BASE_DELAY, heartbeat_fn)
        except Exception as exc:
            exc_str = str(exc).lower()
            if not any(marker in exc_str for marker in TRANSIENT_MARKERS):
                raise
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise UpstreamUnavailable(
                    f"Gemini still unavailable after {attempt} attempts: {exc}"
                ) from exc
            delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
            log.warning(
                "Gemini error (attempt %d/%d): %s. Retrying in %ds...",
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            _sleep_with_heartbeat(delay, heartbeat_fn)


def _transcribe_chunk(
    client: genai.Client,
    chunk_path: str,
    seg_num: int,
    total_segs: int,
    start_sec: float,
    end_sec: float,
    conversation_name: str,
    call_date: str,
    model: str = "gemini-2.5-flash-lite",
    speaker_events: list[dict] | None = None,
    participant_names: list[str] | None = None,
    heartbeat_fn=None,
) -> str:
    """Transcribe a single audio chunk."""
    with open(chunk_path, "rb") as f:
        audio_bytes = f.read()

    timeline_block = _format_timeline_block(
        speaker_events, start_sec=start_sec, end_sec=end_sec
    )
    prompt = SEGMENT_PROMPT.format(
        seg_num=seg_num,
        total_segs=total_segs,
        conversation_name=conversation_name,
        date=call_date,
        start_min=int(start_sec / 60),
        end_min=int(end_sec / 60),
        timeline_block=timeline_block,
        participants_block=_format_participants_block(participant_names),
    )

    log.info(
        "Transcribing segment %d/%d (%.0f-%.0f min, %d KB)",
        seg_num,
        total_segs,
        start_sec / 60,
        end_sec / 60,
        len(audio_bytes) / 1024,
    )

    text = _gemini_call(
        client,
        [
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
            prompt,
        ],
        max_output_tokens=SEGMENT_MAX_OUTPUT_TOKENS,
        model=model,
        heartbeat_fn=heartbeat_fn,
    )

    log.info("Segment %d/%d: %d chars", seg_num, total_segs, len(text))
    return text


def _prepend_coverage_warning(
    notes: str,
    failed_segments: list[tuple[int, float, float, str]],
    total_segments: int,
) -> str:
    """State plainly, at the top of the notes, which minutes are missing.

    The synthesis prompt tells the model to use only what the transcripts
    contain, so it silently omits the placeholder left by a failed segment.
    That turns a partial transcript into notes that look complete. This adds
    the disclosure in code, where it cannot be reasoned away.
    """
    if not failed_segments:
        return notes

    minutes = sum(int((end - start) / 60) for _, start, end, _ in failed_segments)
    lines = [
        "> **⚠ Incomplete transcript.** "
        f"{len(failed_segments)} of {total_segments} segments "
        f"(~{minutes} minutes) could not be transcribed, so the notes below "
        "do not cover the whole call.",
        ">",
    ]
    for seg_num, start, end, reason in failed_segments:
        lines.append(
            f"> - Segment {seg_num}: minutes {int(start / 60)}–{int(end / 60)} — {reason}"
        )
    # No promise of a later fix: once notes exist the caller deletes the
    # recording, and a segment that fails this way (an empty answer on the
    # same input) would fail the same way again.
    lines.append("> \n> What was said in those minutes is not in these notes.")
    return "\n".join(lines) + "\n\n" + notes


def summarize_transcript(
    api_key: str,
    transcript: str,
    conversation_name: str,
    call_date: str,
    duration_min: int,
    participant_names: list[str] | None = None,
    model: str = "gemini-2.5-flash-lite",
    heartbeat_fn=None,
) -> str:
    """One text-only Gemini call: an already-attributed transcript → notes."""
    client = genai.Client(api_key=api_key)
    prompt = _synthesis_prompt(
        transcript, conversation_name, call_date, duration_min, participant_names
    )
    log.info("Synthesizing notes from a %d-char transcript", len(transcript))
    notes = _gemini_call(
        client, [prompt], max_output_tokens=16384, model=model, heartbeat_fn=heartbeat_fn
    )
    log.info("Final notes: %d chars", len(notes))
    return notes


def _synthesis_prompt(
    transcript: str,
    conversation_name: str,
    call_date: str,
    duration_min: int,
    participant_names: list[str] | None,
) -> str:
    return TRANSCRIPT_SYNTHESIS_PROMPT.format(
        conversation_name=conversation_name,
        date=call_date,
        duration_min=int(duration_min),
        participants_block=_format_participants_block(participant_names),
        transcript=transcript,
    )


def summarize_transcript_claude(
    token: str,
    transcript: str,
    conversation_name: str,
    call_date: str,
    duration_min: int,
    participant_names: list[str] | None = None,
    model: str = "haiku",
    heartbeat_fn=None,
) -> str:
    """The same synthesis prompt, answered by Claude through the Claude Code
    CLI on a subscription instead of by Gemini. Only text leaves the box."""
    prompt = _synthesis_prompt(
        transcript, conversation_name, call_date, duration_min, participant_names
    )
    log.info(
        "Synthesizing notes with Claude (%s) from a %d-char transcript",
        model,
        len(transcript),
    )
    notes = claude_call(prompt, model=model, token=token, heartbeat_fn=heartbeat_fn)
    log.info("Final notes: %d chars", len(notes))
    return notes


def _no_speech_notes(conversation_name: str, call_date: str, duration_min: int) -> str:
    return (
        f"# Meeting Notes: {conversation_name}\n"
        f"**Date:** {call_date}\n"
        f"**Duration:** ~{int(duration_min)} minutes\n\n"
        "No speech was detected in this recording, so there are no notes.\n"
    )


def make_notes(
    cfg,
    meta: dict,
    audio_path: str,
    conversation_name: str,
    call_date: str | None,
    participant_names: list[str] | None = None,
    heartbeat_fn=None,
) -> str:
    """Produce notes for one recording by the configured backend.

    ``meta`` is the mutable bookkeeping the caller persists with a retained
    recording: ``speaker_events`` in, ``transcript`` / ``whisper_failures``
    out, ``attempts`` from retained.drain(). Raises WhisperFailed or
    UpstreamUnavailable; the caller retains the recording either way.
    """
    speaker_events = meta.get("speaker_events") or []
    transcript = meta.get("transcript")
    if transcript is None and cfg.transcription_backend != "whisper":
        return transcribe_and_summarize(
            cfg.gemini_api_key,
            audio_path,
            conversation_name,
            model=cfg.gemini_model,
            speaker_events=speaker_events,
            participant_names=participant_names,
            heartbeat_fn=heartbeat_fn,
            call_date=call_date,
        )

    call_date = call_date or date.today().isoformat()
    duration_min = int(_get_audio_duration(audio_path) / 60)
    if transcript is None:
        failures = int(meta.get("whisper_failures", 0))
        attempts = int(meta.get("attempts", 0))
        gave_up = failures >= WHISPER_MAX_FAILURES or attempts > WHISPER_MAX_FAILURES
        if gave_up and cfg.notes_backend == "claude":
            # Claude takes no audio, and the point of this backend is that
            # nothing goes to Google. Give Whisper a couple more tries, then
            # park the recording for a human (reprocess.py).
            if (
                failures >= CLAUDE_WHISPER_MAX_FAILURES
                or attempts > CLAUDE_WHISPER_MAX_FAILURES
            ):
                meta["parked"] = True
                log.error(
                    "Whisper failed %d time(s) over %d attempt(s) on %s; parking it "
                    "(NOTES_BACKEND=claude has no audio fallback). Recover by hand "
                    "with reprocess.py before the 30-day prune deletes it",
                    failures,
                    attempts,
                    audio_path,
                )
                raise WhisperFailed(f"parked after {failures} Whisper failure(s)")
            log.warning(
                "Whisper failed %d time(s) over %d attempt(s) on this recording; "
                "NOTES_BACKEND=claude, so not falling back to Gemini audio",
                failures,
                attempts,
            )
        elif gave_up:
            log.warning(
                "Whisper failed %d time(s) over %d attempt(s) on this recording; "
                "using Gemini audio transcription for it instead",
                failures,
                attempts,
            )
            return transcribe_and_summarize(
                cfg.gemini_api_key,
                audio_path,
                conversation_name,
                model=cfg.gemini_model,
                speaker_events=speaker_events,
                participant_names=participant_names,
                heartbeat_fn=heartbeat_fn,
                call_date=call_date,
            )
        try:
            segments = whisper_transcribe(
                audio_path,
                model_name=cfg.whisper_model,
                model_dir=cfg.whisper_model_dir,
                language=cfg.whisper_language,
                heartbeat_fn=heartbeat_fn,
            )
        except WhisperFailed:
            meta["whisper_failures"] = failures + 1
            raise
        try:
            transcript = label_transcript(segments, speaker_events)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "Speaker timeline unusable (%s: %s); keeping the transcript unlabelled",
                type(exc).__name__,
                exc,
            )
            transcript = label_transcript(segments, [])
        transcript = transcript or NO_SPEECH_SENTINEL
        meta["transcript"] = transcript
        log.info("Whisper transcript: %d chars", len(transcript))

    if transcript == NO_SPEECH_SENTINEL:
        log.warning("No speech detected in %s; writing placeholder notes", audio_path)
        return _no_speech_notes(conversation_name, call_date, duration_min)

    if cfg.notes_backend == "claude":
        return summarize_transcript_claude(
            cfg.claude_oauth_token,
            transcript,
            conversation_name,
            call_date,
            duration_min=duration_min,
            participant_names=participant_names,
            model=cfg.claude_model,
            heartbeat_fn=heartbeat_fn,
        )

    return summarize_transcript(
        cfg.gemini_api_key,
        transcript,
        conversation_name,
        call_date,
        duration_min=duration_min,
        participant_names=participant_names,
        model=cfg.gemini_model,
        heartbeat_fn=heartbeat_fn,
    )


def transcribe_and_summarize(
    api_key: str,
    audio_path: str,
    conversation_name: str,
    model: str = "gemini-2.5-flash-lite",
    speaker_events: list[dict] | None = None,
    participant_names: list[str] | None = None,
    heartbeat_fn=None,
    call_date: str | None = None,
) -> str:
    """Transcribe audio via chunked pipeline: split → transcribe segments → synthesize notes.

    ``heartbeat_fn`` is called between chunks. Transcribing a long call takes
    many minutes on the single-threaded poll loop, and without it the
    container's healthcheck goes stale during normal operation.

    ``call_date`` (ISO ``YYYY-MM-DD``) dates the notes, the email subject and
    the prompt. It defaults to today, which is right for a live call and
    wrong for a retained one retried days later - pass the call's date.

    Raises UpstreamUnavailable if Gemini stays down through a whole retry
    budget; the caller keeps the recording for a later attempt.
    """
    client = genai.Client(api_key=api_key)

    def _beat():
        if heartbeat_fn is not None:
            try:
                heartbeat_fn()
            except Exception:
                log.debug("Heartbeat callback failed", exc_info=True)

    call_date = call_date or date.today().isoformat()

    # Get duration
    duration = _get_audio_duration(audio_path)
    log.info("Audio duration: %.0fs (%.1f min)", duration, duration / 60)

    # Short recordings (under 10 min): single-pass transcription
    if duration <= 600:
        log.info("Short recording, using single-pass transcription")
        return _single_pass_transcribe(
            client,
            audio_path,
            conversation_name,
            call_date,
            duration,
            model=model,
            speaker_events=speaker_events,
            participant_names=participant_names,
            heartbeat_fn=heartbeat_fn,
        )

    # Long recordings: chunked pipeline
    chunk_dir = tempfile.mkdtemp(prefix="notetaker-chunks-")
    chunks = []
    try:
        # Split into 15-minute MP3 chunks
        chunks = _split_to_chunks(audio_path, duration, chunk_dir)
        log.info(
            "Split into %d chunks of %d min each", len(chunks), CHUNK_DURATION // 60
        )

        # Transcribe each chunk with quality validation
        segment_texts = []
        # Segments that produced no transcript. The notes must say so: a gap
        # the reader cannot see is worse than one they can plan around.
        failed_segments: list[tuple[int, float, float, str]] = []
        for i, (chunk_path, start, end) in enumerate(chunks):
            chunk_duration_min = (end - start) / 60
            _beat()
            # A segment that cannot be transcribed must cost only itself.
            # Before 2026-08-25 this exception escaped the loop and killed
            # the whole meeting: three of six segments had already come back
            # with 14887, 17529 and 19350 chars of good transcript, and all
            # of it was discarded because segment 4 came back empty.
            try:
                text = _transcribe_chunk(
                    client,
                    chunk_path,
                    i + 1,
                    len(chunks),
                    start,
                    end,
                    conversation_name,
                    call_date,
                    model=model,
                    speaker_events=speaker_events,
                    participant_names=participant_names,
                    heartbeat_fn=heartbeat_fn,
                )

                # Quality gate: check transcript density (chars per minute)
                chars_per_min = len(text) / max(chunk_duration_min, 0.1)
                if chars_per_min < MIN_CHARS_PER_MINUTE and "[NO SPEECH" not in text:
                    log.warning(
                        "Segment %d/%d too sparse: %d chars for %.0f min (%.0f chars/min, "
                        "threshold %d). Retrying with same model...",
                        i + 1,
                        len(chunks),
                        len(text),
                        chunk_duration_min,
                        chars_per_min,
                        MIN_CHARS_PER_MINUTE,
                    )
                    time.sleep(3)
                    _beat()
                    text = _transcribe_chunk(
                        client,
                        chunk_path,
                        i + 1,
                        len(chunks),
                        start,
                        end,
                        conversation_name,
                        call_date,
                        model=model,
                        speaker_events=speaker_events,
                        participant_names=participant_names,
                        heartbeat_fn=heartbeat_fn,
                    )
                    chars_per_min = len(text) / max(chunk_duration_min, 0.1)

                    if (
                        chars_per_min < MIN_CHARS_PER_MINUTE
                        and "[NO SPEECH" not in text
                    ):
                        log.warning(
                            "Segment %d/%d still sparse after retry: %.0f chars/min. "
                            "Retrying with fallback model %s...",
                            i + 1,
                            len(chunks),
                            chars_per_min,
                            FALLBACK_MODEL,
                        )
                        time.sleep(3)
                        _beat()
                        text = _transcribe_chunk(
                            client,
                            chunk_path,
                            i + 1,
                            len(chunks),
                            start,
                            end,
                            conversation_name,
                            call_date,
                            model=FALLBACK_MODEL,
                            speaker_events=speaker_events,
                            participant_names=participant_names,
                            heartbeat_fn=heartbeat_fn,
                        )
                        chars_per_min = len(text) / max(chunk_duration_min, 0.1)
                        if (
                            chars_per_min < MIN_CHARS_PER_MINUTE
                            and "[NO SPEECH" not in text
                        ):
                            log.warning(
                                "Segment %d/%d still sparse after fallback: %.0f chars/min. "
                                "Using best result available.",
                                i + 1,
                                len(chunks),
                                chars_per_min,
                            )

            except UpstreamUnavailable:
                # Not this segment's fault: Gemini is gone. Notes made now
                # would have a hole the recording could have filled, and
                # the caller deletes the recording once notes exist.
                raise
            except Exception as exc:
                log.error(
                    "Segment %d/%d could not be transcribed (%s). "
                    "Continuing without it so the rest of the meeting survives.",
                    i + 1,
                    len(chunks),
                    exc,
                )
                failed_segments.append((i + 1, start, end, str(exc)))
                text = (
                    f"[SEGMENT {i + 1} OF {len(chunks)} UNAVAILABLE — covers "
                    f"minutes {int(start / 60)}-{int(end / 60)} of the call. "
                    f"Transcription failed: {exc}]"
                )
            segment_texts.append(text)
            # Brief pause between API calls to avoid rate limits
            if i < len(chunks) - 1:
                time.sleep(2)

        # Synthesize final notes from all segments
        all_segments = "\n\n".join(segment_texts)
        log.info(
            "Synthesizing final notes from %d segments (%d chars total)",
            len(segment_texts),
            len(all_segments),
        )

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            conversation_name=conversation_name,
            date=call_date,
            duration_min=int(duration / 60),
            segments=all_segments,
            timeline_block=_format_timeline_block(speaker_events),
            participants_block=_format_participants_block(participant_names),
        )

        notes = _gemini_call(
            client,
            [synthesis_prompt],
            max_output_tokens=16384,
            model=model,
            heartbeat_fn=heartbeat_fn,
        )
        log.info("Final notes: %d chars", len(notes))
        return _prepend_coverage_warning(notes, failed_segments, len(chunks))

    finally:
        # Cleanup chunk files
        for path, _, _ in chunks:
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(chunk_dir)
        except OSError:
            pass


def _single_pass_transcribe(
    client: genai.Client,
    audio_path: str,
    conversation_name: str,
    call_date: str,
    duration: float,
    model: str = "gemini-2.5-flash-lite",
    speaker_events: list[dict] | None = None,
    participant_names: list[str] | None = None,
    heartbeat_fn=None,
) -> str:
    """Single-pass transcription for short recordings. Converts to MP3 first."""
    # Convert to MP3 for reliability (webm format causes issues with Gemini)
    mp3_path = audio_path + ".tmp.mp3"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                audio_path,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                mp3_path,
            ],
            capture_output=True,
        )

        with open(mp3_path, "rb") as f:
            audio_bytes = f.read()

        prompt = SYNTHESIS_PROMPT.format(
            conversation_name=conversation_name,
            date=call_date,
            duration_min=int(duration / 60),
            segments="[Single recording — no segmentation needed. Transcribe and produce notes directly from the audio.]",
            timeline_block=_format_timeline_block(speaker_events),
            participants_block=_format_participants_block(participant_names),
        )

        log.info("Single-pass: sending %d KB MP3 to Gemini", len(audio_bytes) / 1024)
        text = _gemini_call(
            client,
            [
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
                prompt,
            ],
            max_output_tokens=16384,
            model=model,
            heartbeat_fn=heartbeat_fn,
        )
        log.info("Single-pass transcription: %d chars", len(text))
        return text
    finally:
        try:
            os.unlink(mp3_path)
        except OSError:
            pass
