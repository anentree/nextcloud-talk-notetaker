"""Tests for the speaker-attribution pipeline:

- _format_timeline_block (transcriber): formatting, overlap detection, windowing
- resolve_stream_labels (recorder): DOM/ordinal/Speaker N resolution
"""

from __future__ import annotations

import sys
import types

# Stub google.genai before importing transcriber
if "google" not in sys.modules:
    sys.modules["google"] = types.ModuleType("google")
    sys.modules["google.genai"] = types.ModuleType("genai")
    sys.modules["google.genai.types"] = types.ModuleType("types")
    sys.modules["google.genai"].Client = object
    sys.modules["google.genai"].types = sys.modules["google.genai.types"]
    sys.modules["google.genai.types"].Part = object
    sys.modules["google.genai.types"].GenerateContentConfig = object

from notetaker.recorder import resolve_stream_labels  # noqa: E402
from notetaker.transcriber import _format_timeline_block  # noqa: E402


# ---------- _format_timeline_block ----------


def test_timeline_empty_returns_empty_string():
    assert _format_timeline_block(None) == ""
    assert _format_timeline_block([]) == ""


def test_timeline_single_speaker():
    out = _format_timeline_block([{"start_ms": 0, "end_ms": 5000, "label": "Alex"}])
    assert "Participants: Alex" in out
    assert "[00:00.0-00:05.0] Alex" in out


def test_timeline_two_speakers_serial():
    out = _format_timeline_block(
        [
            {"start_ms": 0, "end_ms": 5000, "label": "Alex"},
            {"start_ms": 5000, "end_ms": 10000, "label": "Brantley"},
        ]
    )
    assert "Participants: Alex, Brantley" in out
    assert "[00:00.0-00:05.0] Alex" in out
    assert "[00:05.0-00:10.0] Brantley" in out
    assert "overlap" not in out


def test_timeline_overlap_detection():
    out = _format_timeline_block(
        [
            {"start_ms": 0, "end_ms": 6000, "label": "Alex"},
            {"start_ms": 3000, "end_ms": 9000, "label": "Brantley"},
        ]
    )
    # Three regions: A only, A+B overlap, B only
    assert "[00:00.0-00:03.0] Alex" in out
    assert "[overlap: Alex+Brantley]" in out
    assert "[00:06.0-00:09.0] Brantley" in out


def test_timeline_window_filter_rebases_to_zero():
    out = _format_timeline_block(
        [
            {"start_ms": 0, "end_ms": 7200, "label": "Brantley"},
            {"start_ms": 7400, "end_ms": 12800, "label": "Alex"},
            {"start_ms": 12800, "end_ms": 34100, "label": "Brantley"},
        ],
        start_sec=0,
        end_sec=20,
    )
    assert "[00:00.0-00:07.2] Brantley" in out
    assert "[00:07.4-00:12.8] Alex" in out
    assert "[00:12.8-00:20.0] Brantley" in out


def test_timeline_window_outside_range_returns_empty():
    out = _format_timeline_block(
        [{"start_ms": 0, "end_ms": 5000, "label": "Alex"}],
        start_sec=60,
        end_sec=120,
    )
    assert out == ""


def test_timeline_contiguous_same_speaker_merges():
    out = _format_timeline_block(
        [
            {"start_ms": 0, "end_ms": 3000, "label": "Alex"},
            {"start_ms": 3000, "end_ms": 6000, "label": "Alex"},
        ]
    )
    # Should produce one merged interval, not two
    assert out.count("Alex") == 2  # Once in header, once in body
    assert "[00:00.0-00:06.0] Alex" in out


# ---------- resolve_stream_labels ----------


def test_resolve_all_dom_labeled():
    labels, known, ord_, sn = resolve_stream_labels(
        ["s1", "s2"],
        {"s1": "Alex", "s2": "Brantley"},
        ["Alex", "Brantley"],
    )
    assert labels == {"s1": "Alex", "s2": "Brantley"}
    assert (known, ord_, sn) == (2, 0, 0)


def test_resolve_unambiguous_ordinal_fills_one():
    labels, known, ord_, sn = resolve_stream_labels(
        ["s1", "s2"],
        {"s1": "Alex", "s2": None},
        ["Alex", "Brantley"],
    )
    assert labels == {"s1": "Alex", "s2": "Brantley"}
    assert (known, ord_, sn) == (1, 1, 0)


def test_resolve_no_dom_two_streams_uses_speaker_n_not_guess():
    """With ≥2 ambiguous unlabeled streams we must NOT guess names — use Speaker N."""
    labels, known, ord_, sn = resolve_stream_labels(
        ["s1", "s2"],
        {"s1": None, "s2": None},
        ["Alex", "Brantley"],
    )
    assert labels == {"s1": "Speaker 1", "s2": "Speaker 2"}
    assert (known, ord_, sn) == (0, 0, 2)


def test_resolve_dom_collision_prevented():
    """H2 regression: a DOM-labeled name must not be re-used by ordinal fallback."""
    # 3 streams: s1 DOM=Brantley. Two unlabeled. Two remaining names but one
    # is "Brantley" which is already claimed.
    labels, known, ord_, sn = resolve_stream_labels(
        ["s1", "s2", "s3"],
        {"s1": "Brantley", "s2": None, "s3": None},
        ["Alex", "Brantley", "Carla"],
    )
    assert labels["s1"] == "Brantley"
    # 2 unlabeled streams, 2 remaining names → ambiguous → Speaker N
    assert labels["s2"] == "Speaker 1"
    assert labels["s3"] == "Speaker 2"
    # Brantley appears exactly once
    assert sum(1 for v in labels.values() if v == "Brantley") == 1
    assert (known, ord_, sn) == (1, 0, 2)


def test_resolve_unambiguous_with_one_dom_and_one_unlabeled():
    """1 DOM-labeled + 1 unlabeled + 1 remaining name = unambiguous, fill it."""
    labels, known, ord_, sn = resolve_stream_labels(
        ["s1", "s2"],
        {"s1": "Brantley", "s2": None},
        ["Alex", "Brantley"],
    )
    assert labels == {"s1": "Brantley", "s2": "Alex"}
    assert (known, ord_, sn) == (1, 1, 0)


def test_resolve_no_participants_falls_back_to_speaker_n():
    labels, known, ord_, sn = resolve_stream_labels(["s1"], {"s1": None}, [])
    assert labels == {"s1": "Speaker 1"}
    assert (known, ord_, sn) == (0, 0, 1)


def test_resolve_signaling_labels_take_priority():
    """Signaling labels are merged into dom_labels before calling resolve.
    When all tracks have signaling labels, ordinal/Speaker N are not used."""
    labels, known, ord_, sn = resolve_stream_labels(
        ["t1", "t2", "t3"],
        {"t1": "Alice", "t2": "Bob", "t3": "Carla"},
        ["Alice", "Bob", "Carla"],
    )
    assert labels == {"t1": "Alice", "t2": "Bob", "t3": "Carla"}
    assert (known, ord_, sn) == (3, 0, 0)


def test_resolve_mixed_signaling_and_unlabeled():
    """Some tracks have signaling labels, some don't. Unlabeled uses ordinal if unambiguous."""
    labels, known, ord_, sn = resolve_stream_labels(
        ["t1", "t2", "t3"],
        {"t1": "Alice", "t2": "Bob", "t3": None},
        ["Alice", "Bob", "Carla"],
    )
    assert labels == {"t1": "Alice", "t2": "Bob", "t3": "Carla"}
    assert (known, ord_, sn) == (2, 1, 0)
