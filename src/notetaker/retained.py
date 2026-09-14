"""Durable retry queue for recordings whose notes could not be produced.

The sibling of spool.py, one stage earlier in the pipeline. spool.py keeps a
finished email until the mail server takes it; this module keeps a finished
*recording* until Gemini, Nextcloud and the mail server have all taken it.

Before 2026-09-14, main.py moved such a recording into retained-audio/ and
logged "for manual reprocessing" - and that was the end of it. The 2026-09-08
Accounting Talk recording sat there for six days. Everything needed to finish
it (who was on the call and their addresses, the speaker timeline, the call
start time) had been computed during the call and thrown away, so even the
manual path (reprocess.py) could only produce a weaker, unaddressed result.

Flow:
    retain()  -> moves the .webm into <retained_dir> and writes <name>.webm.json
                 beside it with everything _finish_recording() needs
    drain()   -> called every poll cycle; retries at most ONE due recording,
                 deletes both files on success, backs off on failure

Give-up is age-based and lives in main.prune_retained_audio(), which sweeps
the sidecar along with the audio.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".json"

# First retry waits long enough for an upstream "high demand" spike to pass
# (the 2026-09-08 one ran at least 2.5 minutes; _gemini_call already waited
# out ~6.5 of them before giving up). Later retries double, capped so a
# day-long outage still gets a few attempts a day and a quota reset at
# midnight Pacific is caught within hours.
INITIAL_BACKOFF_SECONDS = 15 * 60
MAX_BACKOFF_SECONDS = 6 * 3600


def sidecar_path(audio_path: str) -> str:
    return audio_path + SIDECAR_SUFFIX


def retain(
    retained_dir: str, audio_path: str, meta: dict, now: float | None = None
) -> str:
    """Move the recording into ``retained_dir`` and write its sidecar.

    ``meta`` carries the call context (token, name, call_start, speaker_events,
    attendee_ids, participants). The destination keeps the
    ``<token>-<original name>`` shape that reprocess.py parses.
    Returns the retained audio path.
    """
    now = time.time() if now is None else now
    os.makedirs(retained_dir, exist_ok=True)
    dest = os.path.join(
        retained_dir, f"{meta.get('token', 'unknown')}-{os.path.basename(audio_path)}"
    )
    shutil.move(audio_path, dest)

    record = dict(meta)
    record.setdefault("attempts", 0)
    record.setdefault("created_at", now)
    record.setdefault("next_attempt_at", now + INITIAL_BACKOFF_SECONDS)
    _atomic_write(sidecar_path(dest), record)
    return dest


def _atomic_write(path: str, record: dict) -> None:
    """Write via temp file + rename so a crash can't leave a partial sidecar."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pending(retained_dir: str) -> list[str]:
    """Sidecar paths, oldest first. Recordings without a sidecar predate this
    module and are not listed; they still need reprocess.py by hand."""
    if not os.path.isdir(retained_dir):
        return []
    paths = [
        os.path.join(retained_dir, n)
        for n in os.listdir(retained_dir)
        if n.endswith(SIDECAR_SUFFIX)
    ]
    return sorted(paths)


def _backoff_for(attempts: int) -> float:
    return min(
        INITIAL_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS
    )


def drain(retained_dir: str, process_fn, now: float | None = None) -> tuple[int, int]:
    """Retry the oldest due recording, if any.

    ``process_fn(meta, audio_path)`` must return True once the notes are in
    durable storage (the same contract as main._process_recording) and may
    raise. One recording per call: a retry transcribes a whole meeting on the
    single-threaded poll loop, so chaining them would hide a live call for as
    long as the backlog takes.

    Returns (finished, remaining).
    """
    now = time.time() if now is None else now
    for path in pending(retained_dir):
        audio = path[: -len(SIDECAR_SUFFIX)]
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            log.exception("Unreadable retained sidecar, skipping: %s", path)
            continue

        if not os.path.exists(audio):
            # prune_retained_audio() took the recording; nothing left to do.
            log.warning("Retained sidecar has no recording, dropping: %s", path)
            _unlink(path)
            continue

        if now < float(meta.get("next_attempt_at", 0)):
            continue

        attempt = int(meta.get("attempts", 0)) + 1
        log.info(
            "Retrying retained recording %s (attempt %d, retained %.1fh ago)",
            audio,
            attempt,
            (now - float(meta.get("created_at", now))) / 3600,
        )
        try:
            ok = process_fn(meta, audio)
        except Exception as exc:
            ok = False
            meta["last_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("Retained recording retry %d raised: %s", attempt, exc)

        if ok:
            _unlink(path)
            _unlink(audio)
            log.info("Retained recording finished and removed: %s", audio)
            return 1, len(pending(retained_dir))

        delay = _backoff_for(attempt)
        meta["attempts"] = attempt
        meta["next_attempt_at"] = now + delay
        meta.setdefault("last_error", "process_fn returned False")
        _atomic_write(path, meta)
        log.warning(
            "Retained recording still not finished (attempt %d); next try in %.0f min",
            attempt,
            delay / 60,
        )
        return 0, len(pending(retained_dir))

    return 0, len(pending(retained_dir))


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        log.warning("Failed to remove retained file: %s", path)
