"""Durable outbox for notes emails.

A meeting's notes email is the only pipeline output with a human waiting on
it, and it is produced once, at the end of a call that may have run for
hours. If the send fails there is nothing to retry from unless the message
was written down first. This module is that written-down copy.

Flow:
    enqueue()  -> writes a .json envelope into <spool_dir>
    drain()    -> re-attempts every envelope, deletes the ones that succeed

drain() is called at the top of each poll cycle, so a transient mail outage
self-heals within one poll interval without any human action.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid

log = logging.getLogger(__name__)

# Give-up policy is AGE-based, not attempt-based. drain() runs every poll
# cycle (10s), so an attempt counter would be exhausted within minutes — and
# the outage this module exists for lasted months.
MAX_AGE_SECONDS = 7 * 24 * 3600

# Exponential backoff between retries, so a long outage costs a handful of
# attempts per hour instead of one every ten seconds. Each failed attempt
# also blocks the poll loop for the SMTP timeout, so pacing matters.
INITIAL_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600


def enqueue(spool_dir: str, envelope: dict) -> str:
    """Persist a failed email for later retry. Returns the file path."""
    os.makedirs(spool_dir, exist_ok=True)
    envelope = dict(envelope)
    envelope.setdefault("attempts", 0)
    envelope.setdefault("created_at", time.time())
    envelope.setdefault("next_attempt_at", 0)

    name = f"{int(envelope['created_at'])}-{uuid.uuid4().hex[:8]}.json"
    path = os.path.join(spool_dir, name)
    _atomic_write(path, envelope)
    log.warning("Queued undeliverable notes email for retry: %s", path)
    return path


def _atomic_write(path: str, envelope: dict) -> None:
    """Write via temp file + rename so a crash can't leave a partial envelope."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pending(spool_dir: str) -> list[str]:
    """List queued envelope paths, oldest first."""
    if not os.path.isdir(spool_dir):
        return []
    paths = [
        os.path.join(spool_dir, n)
        for n in os.listdir(spool_dir)
        if n.endswith(".json")
    ]
    return sorted(paths)


def _backoff_for(attempts: int) -> float:
    return min(INITIAL_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS)


# Cap work per cycle. Each attempt can block for the SMTP timeout, and
# drain() runs on the single-threaded poll loop — a backlog of 20 envelopes
# against a blackholed host would stall call detection for ten minutes.
MAX_ATTEMPTS_PER_DRAIN = 3


def drain(spool_dir: str, send_fn, now: float | None = None) -> tuple[int, int]:
    """Retry queued emails that are due.

    ``send_fn(envelope)`` should raise on total failure, or return a residual
    envelope (falsy when fully delivered) so partially-delivered messages are
    not re-sent to recipients who already got them.

    Returns (sent, remaining).
    """
    now = time.time() if now is None else now
    paths = pending(spool_dir)
    if not paths:
        return 0, 0

    sent = 0
    attempted = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                envelope = json.load(f)
        except Exception:
            log.exception("Unreadable spool envelope, skipping: %s", path)
            continue

        # Age is checked BEFORE the backoff window. Otherwise an envelope
        # whose next attempt is scheduled past the expiry point is skipped
        # forever and never retired.
        age = now - float(envelope.get("created_at", now))
        if age > MAX_AGE_SECONDS:
            failed = path + ".failed"
            envelope["gave_up_at"] = now
            _atomic_write(failed, envelope)
            _unlink(path)
            log.error(
                "Giving up on queued email after %.1f days (kept at %s) — "
                "the notes are still in this file and in Nextcloud",
                age / 86400,
                failed,
            )
            continue

        if now < float(envelope.get("next_attempt_at", 0)):
            continue

        if attempted >= MAX_ATTEMPTS_PER_DRAIN:
            log.info(
                "Reached the per-cycle retry cap (%d); %d envelope(s) deferred "
                "to the next poll",
                MAX_ATTEMPTS_PER_DRAIN,
                len(paths) - attempted,
            )
            break

        attempted += 1
        try:
            residual = send_fn(envelope)
        except Exception as exc:
            attempts = int(envelope.get("attempts", 0)) + 1
            delay = _backoff_for(attempts)
            envelope["attempts"] = attempts
            envelope["last_error"] = f"{type(exc).__name__}: {exc}"
            envelope["next_attempt_at"] = now + delay
            _atomic_write(path, envelope)
            log.warning(
                "Queued email retry %d failed (%s); next attempt in %.0fs",
                attempts,
                exc,
                delay,
            )
            continue

        if residual:
            attempts = int(envelope.get("attempts", 0)) + 1
            delay = _backoff_for(attempts)
            residual["attempts"] = attempts
            residual["next_attempt_at"] = now + delay
            residual.setdefault("created_at", envelope.get("created_at", now))
            _atomic_write(path, residual)
            log.warning(
                "Queued email partially delivered; %d recipient(s) still "
                "pending, next attempt in %.0fs",
                len(residual.get("recipients", [])),
                delay,
            )
            continue

        _unlink(path)
        sent += 1
        log.info("Delivered queued notes email: %s", path)

    if attempted:
        log.info(
            "Drained spool %s: attempted %d, delivered %d", spool_dir, attempted, sent
        )
    return sent, len(pending(spool_dir))


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        log.warning("Failed to remove spool file: %s", path)
