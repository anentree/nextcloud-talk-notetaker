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


CHECK_NAMES = ("state-dir", "smtp", "nextcloud", "notes-target", "gemini")


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
    ]
    if not cfg.email_enabled:
        # Nothing to probe, and localhost:25 inside the container is a
        # guaranteed connection refused — the crash loop behind the wizard's
        # default "no email" answer before 2026-09-14.
        log.info(
            "PREFLIGHT skipping smtp: email is disabled because SMTP_HOST is unset"
        )
        checks = [c for c in checks if c[0] != "smtp"]

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
