"""The prompt must never manufacture speakers.

On 2026-08-13 a two-person call (Brantley + Piyush) produced notes naming
four speakers and assigning action items to Shreyash and Yashvi, neither of
whom was on the call. Three things combined to cause it:

  1. participant_names came from ROOM MEMBERSHIP, not call attendance.
  2. The roster block asserted "all were in the call" — which was false.
  3. SYNTHESIS_PROMPT said "ALL known participants should appear in the
     notes. Do not omit anyone", which instructed the model to invent them.

Fixing only the recipient list would have left the notes wrong, so these
assertions pin the prompt itself.
"""

from __future__ import annotations

from notetaker.transcriber import (
    SEGMENT_PROMPT,
    SYNTHESIS_PROMPT,
    _format_participants_block,
)


def test_roster_block_does_not_claim_attendance():
    block = _format_participants_block(["Brantley", "Piyush", "Shreyash", "Yashvi"])
    assert "all were in the call" not in block.lower()
    assert "not proof of attendance" in block.lower()


def test_roster_block_forbids_introducing_unheard_names():
    block = _format_participants_block(["Brantley", "Piyush"])
    assert "never introduce a name" in block.lower()


def test_roster_block_is_empty_without_names():
    assert _format_participants_block([]) == ""
    assert _format_participants_block(None) == ""


def test_synthesis_prompt_no_longer_demands_everyone_appears():
    """The exact instruction that caused the fabrication."""
    assert "ALL known participants should appear" not in SYNTHESIS_PROMPT
    assert "Do not omit anyone from the participant list" not in SYNTHESIS_PROMPT


def test_synthesis_prompt_forbids_attributing_to_absent_people():
    lowered = SYNTHESIS_PROMPT.lower()
    assert "never attribute" in lowered
    assert "roster is not evidence" in lowered


def test_synthesis_prompt_offers_an_unattributed_escape_hatch():
    """Without a safe fallback the model will guess a name."""
    assert "Unattributed" in SYNTHESIS_PROMPT


def test_segment_prompt_keeps_generic_labels_when_unsure():
    lowered = SEGMENT_PROMPT.lower()
    assert "keep the generic labels" in lowered
    assert "must stay unnamed" in lowered


def test_segment_prompt_does_not_match_speaker_count_to_roster():
    assert "Do NOT try to match the length of the participant list" in SEGMENT_PROMPT


def test_transcript_prompt_forbids_attributing_to_absent_people():
    from notetaker.transcriber import TRANSCRIPT_SYNTHESIS_PROMPT

    lowered = TRANSCRIPT_SYNTHESIS_PROMPT.lower()
    assert "never attribute" in lowered
    assert "roster is not evidence" in lowered


def test_transcript_prompt_offers_an_unattributed_escape_hatch():
    from notetaker.transcriber import TRANSCRIPT_SYNTHESIS_PROMPT

    assert "Unattributed" in TRANSCRIPT_SYNTHESIS_PROMPT
    assert "authoritative" in TRANSCRIPT_SYNTHESIS_PROMPT
