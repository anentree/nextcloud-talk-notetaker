from __future__ import annotations

import logging

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

PROMPT_TEMPLATE = """You are a professional meeting note-taker. This audio is from a Nextcloud Talk call named "{conversation_name}".

Analyze the meeting audio and produce structured notes in the following markdown format:

# Meeting: {conversation_name}
**Date:** [Today's date]
**Participants:** [Detected speakers -- use Speaker 1, Speaker 2, etc.]
**Duration:** [Approximate duration]

## Summary
[2-4 paragraph overview of the meeting, organized chronologically by topic]

## Key Takeaways
- [Takeaway 1]
- [Takeaway 2]
- [Takeaway 3]

## Action Items
- **[Speaker/Assignee]:** [Action item description] -- [Deadline if mentioned]

## Decisions Made
- [Decision 1]
- [Decision 2]

## Follow-Up Email

Subject: Meeting Notes -- {conversation_name} -- [Date]

Hi everyone,

Here's a summary of our call today:

[Brief 2-3 sentence summary]

**Action items:**
[List action items with assignees]

Please let me know if I missed anything.

Best,
AI Notetaker

---

Be thorough but concise. Use speaker diarization to attribute statements. If a section has no content (e.g., no action items), write "None identified." instead of omitting the section.
"""


def transcribe_and_summarize(
    api_key: str, audio_path: str, conversation_name: str
) -> str:
    """Send audio to Gemini 2.5 Flash for transcription and summarization."""
    client = genai.Client(api_key=api_key)

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    prompt = PROMPT_TEMPLATE.format(conversation_name=conversation_name)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
            prompt,
        ],
    )

    log.info("Transcription complete for %s (%d chars)", audio_path, len(response.text))
    return response.text
