"""Speaker labels come from the recorder's timeline, never from a guess.

The recorder knows who was talking when (one voice-activity interval list per
participant, from that person's own audio track). Whisper knows what was said
when. Joining the two in code means a name can appear on a line only if that
person's track was active while the words were spoken.
"""

from __future__ import annotations

from notetaker.attribution import UNATTRIBUTED, label_transcript


def seg(start, end, text):
    return {"start": float(start), "end": float(end), "text": text}


def ev(label, start_s, end_s):
    return {"label": label, "start_ms": int(start_s * 1000), "end_ms": int(end_s * 1000)}


def test_single_speaker_owns_every_line():
    out = label_transcript([seg(0, 5, "hello"), seg(10, 15, "again")], [ev("Jo", 0, 20)])
    assert out == "[00:00] Jo: hello\n[00:10] Jo: again"


def test_far_apart_lines_do_not_merge_but_close_ones_do():
    out = label_transcript(
        [seg(0, 5, "hello"), seg(6, 8, "there"), seg(30, 35, "later")],
        [ev("Jo", 0, 40)],
    )
    assert out == "[00:00] Jo: hello there\n[00:30] Jo: later"


def test_two_speakers_alternate():
    events = [ev("Jo", 0, 10), ev("Brantley", 10, 20)]
    out = label_transcript([seg(0, 9, "one"), seg(11, 19, "two")], events)
    assert out == "[00:00] Jo: one\n[00:11] Brantley: two"


def test_segment_straddling_two_speakers_goes_to_the_majority():
    events = [ev("Jo", 0, 10), ev("Brantley", 10, 20)]
    out = label_transcript([seg(7, 17, "shared")], events)  # Jo 3 s, Brantley 7 s
    assert out == "[00:07] Brantley: shared"


def test_below_half_coverage_is_unattributed():
    out = label_transcript([seg(0, 10, "quiet")], [ev("Jo", 0, 4)])
    assert out == f"[00:00] {UNATTRIBUTED}: quiet"


def test_two_people_covering_the_same_segment_are_both_named():
    events = [ev("Jo", 0, 10), ev("Brantley", 0, 10)]
    out = label_transcript([seg(0, 10, "crosstalk")], events)
    assert out in ("[00:00] Jo + Brantley: crosstalk", "[00:00] Brantley + Jo: crosstalk")


def test_one_person_with_overlapping_intervals_is_counted_once():
    """A reconnect gives one person two tracks whose intervals overlap; summing
    them would exceed the segment length and fake an 'A + B' crosstalk line."""
    events = [ev("Jo", 0, 10), ev("Jo", 2, 12), ev("Brantley", 8, 9)]
    out = label_transcript([seg(0, 10, "solo")], events)
    assert out == "[00:00] Jo: solo"


def test_no_timeline_means_no_labels():
    out = label_transcript([seg(0, 5, "hello"), seg(30, 35, "later")], [])
    assert out == "[00:00] hello\n[00:30] later"
    assert label_transcript([seg(0, 5, "hello")], None) == "[00:00] hello"


def test_blank_segments_are_dropped_and_text_is_stripped():
    out = label_transcript([seg(0, 1, "   "), seg(1, 2, "  hi  ")], [ev("Jo", 0, 5)])
    assert out == "[00:01] Jo: hi"


def test_timestamp_format_is_minutes_and_seconds():
    out = label_transcript([seg(3725.4, 3730, "late")], [ev("Jo", 3700, 3800)])
    assert out == "[62:05] Jo: late"


def test_empty_input_is_empty_string():
    assert label_transcript([], [ev("Jo", 0, 5)]) == ""
