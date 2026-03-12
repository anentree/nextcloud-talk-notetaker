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

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds
CHUNK_DURATION = 900  # 15 minutes per chunk

SEGMENT_PROMPT = """You are a professional meeting transcriber. This audio is segment {seg_num} of {total_segs} from a Nextcloud Talk call named "{conversation_name}" on {date}.

This segment covers approximately minutes {start_min} to {end_min} of the call.

CRITICAL RULES:
- Transcribe EVERYTHING said in this segment. Be thorough — do not skip or summarize.
- Only include words that were ACTUALLY SPOKEN. Do NOT invent or hallucinate dialogue.
- If the audio is silent or unintelligible, respond with: "[NO SPEECH IN THIS SEGMENT]"
- Identify speakers by name if spoken, otherwise use Speaker 1, Speaker 2, etc. Be consistent.
- Use today's date ({date}) for any date references.

Produce a detailed transcript of this segment in this format:

## Segment {seg_num} (minutes {start_min}-{end_min})

**Topics covered:** [Brief list of topics in this segment]

**Transcript:**
[Detailed chronological transcript with speaker attribution. Include all discussion points, decisions, action items, and notable statements. Use direct quotes for important statements.]
"""

SYNTHESIS_PROMPT = """You are a professional meeting note-taker. Below are detailed transcripts from all segments of a {duration_min}-minute Nextcloud Talk call named "{conversation_name}" on {date}.

Your job is to synthesize these segment transcripts into comprehensive, well-structured meeting notes.

CRITICAL RULES:
- Use ONLY information from the transcripts below. Do NOT invent or add anything not present.
- Cover the ENTIRE call chronologically — every topic, decision, and action item.
- Use speaker names consistently as identified in the transcripts.
- If the transcripts mention specific numbers, dates, names, or amounts — use them exactly.

Produce the notes in this markdown format:

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

Here's a summary of our call today ({duration_min} minutes):

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

=== SEGMENT TRANSCRIPTS ===

{segments}
"""


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


def _gemini_call(
    client: genai.Client,
    contents: list,
    max_output_tokens: int = 8192,
    model: str = "gemini-2.5-flash-lite",
) -> str:
    """Make a Gemini API call with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
            )
            return response.text
        except Exception as exc:
            is_rate_limit = (
                "429" in str(exc) or "resource_exhausted" in str(exc).lower()
            )
            if is_rate_limit and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning(
                    "Gemini rate limited (attempt %d/%d), retrying in %ds",
                    attempt,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
            else:
                raise


def _transcribe_chunk(
    client: genai.Client,
    chunk_path: str,
    seg_num: int,
    total_segs: int,
    start_sec: float,
    end_sec: float,
    conversation_name: str,
    today: str,
    model: str = "gemini-2.5-flash-lite",
) -> str:
    """Transcribe a single audio chunk."""
    with open(chunk_path, "rb") as f:
        audio_bytes = f.read()

    prompt = SEGMENT_PROMPT.format(
        seg_num=seg_num,
        total_segs=total_segs,
        conversation_name=conversation_name,
        date=today,
        start_min=int(start_sec / 60),
        end_min=int(end_sec / 60),
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
        model=model,
    )

    log.info("Segment %d/%d: %d chars", seg_num, total_segs, len(text))
    return text


def transcribe_and_summarize(
    api_key: str,
    audio_path: str,
    conversation_name: str,
    model: str = "gemini-2.5-flash-lite",
) -> str:
    """Transcribe audio via chunked pipeline: split → transcribe segments → synthesize notes."""
    client = genai.Client(api_key=api_key)
    today = date.today().isoformat()

    # Get duration
    duration = _get_audio_duration(audio_path)
    log.info("Audio duration: %.0fs (%.1f min)", duration, duration / 60)

    # Short recordings (under 10 min): single-pass transcription
    if duration <= 600:
        log.info("Short recording, using single-pass transcription")
        return _single_pass_transcribe(
            client, audio_path, conversation_name, today, duration, model=model
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

        # Transcribe each chunk
        segment_texts = []
        for i, (chunk_path, start, end) in enumerate(chunks):
            text = _transcribe_chunk(
                client,
                chunk_path,
                i + 1,
                len(chunks),
                start,
                end,
                conversation_name,
                today,
                model=model,
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
            date=today,
            duration_min=int(duration / 60),
            segments=all_segments,
        )

        notes = _gemini_call(
            client, [synthesis_prompt], max_output_tokens=16384, model=model
        )
        log.info("Final notes: %d chars", len(notes))
        return notes

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
    today: str,
    duration: float,
    model: str = "gemini-2.5-flash-lite",
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
            date=today,
            duration_min=int(duration / 60),
            segments="[Single recording — no segmentation needed. Transcribe and produce notes directly from the audio.]",
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
        )
        log.info("Single-pass transcription: %d chars", len(text))
        return text
    finally:
        try:
            os.unlink(mp3_path)
        except OSError:
            pass
