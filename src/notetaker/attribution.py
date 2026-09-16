"""Assign speakers to Whisper segments from the recorder's timeline.

The recorder knows who was talking when (one voice-activity interval list
per participant, from that person's own audio track). Whisper knows what
was said when. Joining the two here makes attribution a lookup, not a
guess: a name can appear on a transcript line only if that person's track
was active while the words were spoken, so the model that writes the notes
never sees a name it could misattribute.
"""

from __future__ import annotations

# A speaker must cover at least this share of a segment to own it.
COVERAGE_THRESHOLD = 0.5
# Consecutive same-speaker segments closer than this are joined into one line.
MERGE_GAP_SECONDS = 2.0
UNATTRIBUTED = "Unattributed"

Interval = tuple[float, float]


def _union(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping intervals so one person's two tracks count once."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intervals_by_label(speaker_events: list[dict]) -> dict[str, list[Interval]]:
    by_label: dict[str, list[Interval]] = {}
    for event in speaker_events:
        by_label.setdefault(event["label"], []).append(
            (event["start_ms"] / 1000.0, event["end_ms"] / 1000.0)
        )
    return {label: _union(ivs) for label, ivs in by_label.items()}


def _coverage(seg_start: float, seg_end: float, intervals: list[Interval]) -> float:
    return sum(
        max(0.0, min(seg_end, end) - max(seg_start, start)) for start, end in intervals
    )


def _label_for(seg_start: float, seg_end: float, by_label: dict[str, list[Interval]]) -> str:
    length = seg_end - seg_start
    if length <= 0:
        return UNATTRIBUTED
    ranked = sorted(
        ((_coverage(seg_start, seg_end, ivs) / length, label) for label, ivs in by_label.items()),
        reverse=True,
    )
    best_share, best = ranked[0]
    if best_share < COVERAGE_THRESHOLD:
        return UNATTRIBUTED
    if len(ranked) > 1 and ranked[1][0] >= COVERAGE_THRESHOLD:
        return f"{best} + {ranked[1][1]}"
    return best


def _fmt_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def label_transcript(segments: list[dict], speaker_events: list[dict] | None) -> str:
    """One line per utterance: ``[MM:SS] Name: text``.

    With no timeline the lines carry no name at all, which the synthesis
    prompt reads as "attribution unknown". Roster names never enter here.
    """
    by_label = _intervals_by_label(speaker_events or [])
    lines: list[dict] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start, end = float(segment["start"]), float(segment["end"])
        label = _label_for(start, end, by_label) if by_label else None
        previous = lines[-1] if lines else None
        if (
            previous is not None
            and previous["label"] == label
            and start - previous["end"] < MERGE_GAP_SECONDS
        ):
            previous["text"] += " " + text
            previous["end"] = end
        else:
            lines.append({"start": start, "end": end, "label": label, "text": text})

    out = []
    for line in lines:
        stamp = _fmt_ts(line["start"])
        if line["label"] is None:
            out.append(f"[{stamp}] {line['text']}")
        else:
            out.append(f"[{stamp}] {line['label']}: {line['text']}")
    return "\n".join(out)
