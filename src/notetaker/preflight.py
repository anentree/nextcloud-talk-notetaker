"""Startup validation.

The 2026-08-13 incident: SMTP_HOST held a Tailscale address that had been
dead since a June rebuild. The service booted cleanly, polled happily for
weeks, and the first symptom was a stack trace at minute 79 of a real
meeting — after the audio had already been deleted.

Every dependency this service needs is cheap to test at boot. Testing them
there turns a silent months-long data-loss bug into a container that refuses
to start, which both `docker ps` and the weekly health probe already notice.
"""

from __future__ import annotations

import logging
import smtplib

import requests

from notetaker.config import Config
from notetaker.mailer import _tls_context, upgrade_to_tls

log = logging.getLogger(__name__)

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}


def check_smtp(cfg: Config) -> None:
    """Connect, upgrade to TLS, and authenticate — the full send path."""
    if cfg.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(
            cfg.smtp_host,
            cfg.smtp_port,
            timeout=cfg.smtp_timeout,
            context=_tls_context(cfg.smtp_host, cfg.smtp_tls_verify),
        )
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout)
    with smtp:
        smtp.ehlo()
        # Same rule as the real send path, so preflight cannot pass a
        # configuration that send_notes_email would refuse.
        upgrade_to_tls(
            smtp,
            cfg.smtp_host,
            cfg.smtp_port,
            _tls_context(cfg.smtp_host, cfg.smtp_tls_verify),
            has_credentials=bool(cfg.smtp_user and cfg.smtp_password),
        )
        if cfg.smtp_user and cfg.smtp_password:
            smtp.login(cfg.smtp_user, cfg.smtp_password)


def check_nextcloud(cfg: Config) -> None:
    """Confirm the bot's credentials work against the Talk API."""
    resp = requests.get(
        f"{cfg.nextcloud_url}/ocs/v2.php/apps/spreed/api/v4/room",
        auth=(cfg.nextcloud_user, cfg.nextcloud_password),
        headers=OCS_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()


def check_notes_target(cfg: Config) -> None:
    """Confirm the notes destination is writable."""
    if cfg.notes_storage == "local":
        import os

        os.makedirs(cfg.local_notes_dir, exist_ok=True)
        if not os.access(cfg.local_notes_dir, os.W_OK):
            raise RuntimeError(f"LOCAL_NOTES_DIR not writable: {cfg.local_notes_dir}")
        return

    dav = f"{cfg.nextcloud_url}/remote.php/dav/files/{cfg.nextcloud_user}"
    resp = requests.request(
        "PROPFIND",
        dav,
        auth=(cfg.nextcloud_user, cfg.nextcloud_password),
        headers={"Depth": "0"},
        timeout=15,
    )
    if resp.status_code not in (207, 200):
        raise RuntimeError(f"WebDAV PROPFIND returned {resp.status_code}")


def check_gemini(cfg: Config) -> None:
    """Confirm the API key is valid and the configured model exists."""
    resp = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_model}",
        headers={"x-goog-api-key": cfg.gemini_api_key},
        timeout=15,
    )
    resp.raise_for_status()


def check_state_dir(cfg: Config) -> None:
    """Confirm the spool directory is writable — it is the failure safety net."""
    import os

    os.makedirs(cfg.spool_dir, exist_ok=True)
    if not os.access(cfg.spool_dir, os.W_OK):
        raise RuntimeError(f"Spool directory not writable: {cfg.spool_dir}")


# cgroup v2 first, then v1. "max" (v2) or an absurdly large number (v1's
# no-limit sentinel) means unlimited.
CGROUP_MEMORY_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
)
WHISPER_MIN_MEMORY_BYTES = 4 * 1024**3


def container_memory_limit() -> int | None:
    """The cgroup memory cap in bytes, or None when unlimited or unreadable."""
    for path in CGROUP_MEMORY_LIMIT_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            limit = int(raw)
        except ValueError:
            continue
        if limit >= 1 << 40:  # v1 reports ~2**63 when no limit is set
            return None
        return limit
    return None


def check_whisper(cfg: Config) -> None:
    """Confirm the local model is on disk. Never downloads: 1.6 GB at startup
    would outlast the healthcheck's start period and the container would be
    reported unhealthy while doing the right thing."""
    from notetaker import whisper

    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed in this image; run: docker compose build"
        ) from exc
    try:
        present = whisper.model_is_downloaded(cfg.whisper_model, cfg.whisper_model_dir)
    except ValueError as exc:
        raise RuntimeError(
            f"unknown Whisper model name {cfg.whisper_model!r} (WHISPER_MODEL): {exc}"
        ) from exc
    if not present:
        raise RuntimeError(
            f"Whisper model {cfg.whisper_model!r} is not downloaded to "
            f"{cfg.whisper_model_dir}. Run: {whisper.DOWNLOAD_COMMAND} "
            "— or set TRANSCRIPTION_BACKEND=gemini in .env"
        )

    limit = container_memory_limit()
    if limit is not None and limit < WHISPER_MIN_MEMORY_BYTES:
        raise RuntimeError(
            f"Whisper needs a container memory limit of at least 4g; this container "
            f"has {limit / 1024**3:.1f}g. Set NOTETAKER_MEMORY_LIMIT=6g in .env and "
            "run: docker compose up -d"
        )


CHECK_NAMES = ("state-dir", "smtp", "nextcloud", "notes-target", "gemini", "whisper")


def run_preflight(cfg: Config) -> list[str]:
    """Run every startup check.

    Returns the list of failures; the caller is expected to exit non-zero so
    the container crash-loops visibly rather than pretending to work.
    """
    # Resolved at call time rather than captured in a module-level tuple, so
    # the individual checks stay independently patchable in tests.
    checks = [
        ("state-dir", check_state_dir),
        ("smtp", check_smtp),
        ("nextcloud", check_nextcloud),
        ("notes-target", check_notes_target),
        ("gemini", check_gemini),
        ("whisper", check_whisper),
    ]
    if not cfg.email_enabled:
        # Nothing to probe, and localhost:25 inside the container is a
        # guaranteed connection refused — the crash loop behind the wizard's
        # default "no email" answer before 2026-09-14.
        log.info(
            "PREFLIGHT skipping smtp: email is disabled because SMTP_HOST is unset"
        )
        checks = [c for c in checks if c[0] != "smtp"]

    if cfg.transcription_backend != "whisper":
        log.info("PREFLIGHT skipping whisper: TRANSCRIPTION_BACKEND is %s", cfg.transcription_backend)
        checks = [c for c in checks if c[0] != "whisper"]

    failures: list[str] = []

    for problem in cfg.validate():
        failures.append(f"config: {problem}")
        log.error("PREFLIGHT config: %s", problem)

    for name, fn in checks:
        try:
            fn(cfg)
        except Exception as exc:
            msg = f"{name}: {type(exc).__name__}: {exc}"
            failures.append(msg)
            log.error("PREFLIGHT FAILED %s", msg)
        else:
            log.info("PREFLIGHT ok: %s", name)

    if failures:
        log.error("PREFLIGHT FAILED with %d problem(s)", len(failures))
    else:
        log.info("PREFLIGHT OK — all dependencies reachable")
    return failures
