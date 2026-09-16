"""Preflight decides whether the service starts at all.

It exists because SMTP_HOST held a dead address for months while the
container looked healthy. It must fail on a genuinely broken dependency —
and, just as importantly, it must NOT fail on the working production
configuration, or it turns a silent bug into a crash-loop.
"""

from __future__ import annotations

import contextlib
import ssl
import sys
from unittest.mock import MagicMock, patch

from notetaker.config import Config
from notetaker.mailer import _is_local_target, _tls_context
from notetaker.preflight import check_smtp, container_memory_limit, run_preflight


def make_cfg(**kw) -> Config:
    base = dict(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        smtp_host="host.docker.internal",
        smtp_port=587,
        smtp_from="bot@example.com",
    )
    base.update(kw)
    return Config(**base)


# --- TLS policy: the deploy blocker ----------------------------------------


def test_local_smtp_hop_does_not_require_a_matching_certificate():
    """host.docker.internal is how the container reaches Postfix, but the
    server presents a cert for mail.example.com. Demanding hostname
    verification there fails every send AND the preflight, which would
    crash-loop the container on startup."""
    ctx = _tls_context("host.docker.internal")
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_public_smtp_host_is_verified():
    ctx = _tls_context("smtp.gmail.com")
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_verification_can_be_forced_on():
    ctx = _tls_context("host.docker.internal", "true")
    assert ctx.check_hostname is True


def test_verification_can_be_forced_off():
    ctx = _tls_context("smtp.gmail.com", "false")
    assert ctx.check_hostname is False


def test_local_target_classification():
    for host in (
        "localhost",
        "host.docker.internal",
        "127.0.0.1",
        "192.168.1.10",
        "100.64.0.1",
        "mail.internal",
        "::1",
    ):
        assert _is_local_target(host), host

    # Hostnames made only of hex letters must not be mistaken for IPv6.
    for host in ("smtp.gmail.com", "cafe", "decaf", "beef", "mail.example.com"):
        assert not _is_local_target(host), host


def test_public_ip_literal_is_not_a_local_target():
    """A globally routable IP is a remote host and must be verified.

    Regression: any IPv4/IPv6 literal used to classify as local, so pointing
    SMTP_HOST at a relay's public address silently accepted any certificate.
    """
    for host in ("8.8.8.8", "2606:4700::1111"):
        assert not _is_local_target(host), host


def test_unrecognised_tls_verify_value_fails_closed():
    """Regression: only the literal strings "true"/"auto" verified; anything
    else — including "1" — fell through to CERT_NONE and logged it as
    though the operator had asked for that."""
    # Use a LOCAL host on purpose: under "auto" it would get CERT_NONE, so a
    # verifying context here proves fail-closed, not a fall-through to auto.
    for value in ("maybe", "yse", "verify", "t", "enabled", "1.0"):
        ctx = _tls_context("host.docker.internal", value)
        assert ctx.check_hostname is True, value
        assert ctx.verify_mode == ssl.CERT_REQUIRED, value


def test_validate_accepts_the_same_tls_verify_spellings_the_mailer_does():
    """config.validate() and mailer._tls_context() must agree, or a value
    that passes preflight is treated differently at send time (or vice
    versa — "True" used to fail validation but work under SKIP_PREFLIGHT)."""
    for value in ("auto", "true", "True", "1", "yes", "false", "0", "OFF", " on "):
        assert make_cfg(smtp_tls_verify=value).validate() == [], value
    problems = make_cfg(smtp_tls_verify="maybe").validate()
    assert any("SMTP_TLS_VERIFY" in p for p in problems)


def test_common_true_and_false_spellings_are_honoured():
    for value in ("true", "1", "yes", "on", "True", "YES"):
        assert _tls_context("host.docker.internal", value).check_hostname is True, value
    for value in ("false", "0", "no", "off", "False", "OFF"):
        assert _tls_context("smtp.gmail.com", value).check_hostname is False, value


# --- preflight behaviour ----------------------------------------------------


def test_preflight_reports_a_dead_smtp_host():
    cfg = make_cfg()
    with patch(
        "notetaker.preflight.smtplib.SMTP", side_effect=TimeoutError("timed out")
    ):
        failures = [f for f in run_preflight(cfg) if f.startswith("smtp")]
    assert failures, "a dead SMTP host must fail preflight"


def test_preflight_smtp_authenticates_not_just_connects():
    """A TCP connect alone would not have caught a bad password."""
    cfg = make_cfg(smtp_user="bot", smtp_password="pw")
    with patch("notetaker.preflight.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = True
        check_smtp(cfg)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("bot", "pw")


def test_preflight_passes_the_configured_timeout():
    cfg = make_cfg(smtp_timeout=11)
    with patch("notetaker.preflight.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = False
        check_smtp(cfg)
    assert smtp_cls.call_args.kwargs["timeout"] == 11


def test_preflight_flags_a_hardcoded_ip_smtp_host(tmp_path):
    """The literal cause of the incident: a pinned IP that went stale."""
    cfg = make_cfg(smtp_host="100.103.151.67", state_dir=str(tmp_path))
    problems = cfg.validate()
    assert any("hardcoded IP" in p for p in problems)


def test_preflight_passes_on_a_healthy_config(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    with (
        patch("notetaker.preflight.check_smtp"),
        patch("notetaker.preflight.check_nextcloud"),
        patch("notetaker.preflight.check_notes_target"),
        patch("notetaker.preflight.check_gemini"),
    ):
        assert run_preflight(cfg) == []


def test_preflight_collects_all_failures_not_just_the_first(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    with (
        patch("notetaker.preflight.check_smtp", side_effect=OSError("smtp down")),
        patch("notetaker.preflight.check_nextcloud", side_effect=OSError("nc down")),
        patch("notetaker.preflight.check_notes_target"),
        patch("notetaker.preflight.check_gemini", side_effect=OSError("gemini down")),
    ):
        failures = run_preflight(cfg)
    assert len(failures) == 3


def test_config_validate_accepts_the_production_shape(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    assert cfg.validate() == []


def test_config_validate_rejects_half_configured_credentials():
    cfg = make_cfg(smtp_user="bot", smtp_password="")
    assert any("must be set together" in p for p in cfg.validate())


def test_state_dir_check_creates_the_spool(tmp_path):
    from notetaker.preflight import check_state_dir

    cfg = make_cfg(state_dir=str(tmp_path / "nested"))
    check_state_dir(cfg)
    import os

    assert os.path.isdir(cfg.spool_dir)


def test_gemini_check_uses_the_configured_model():
    cfg = make_cfg(gemini_model="gemini-2.5-flash-lite")
    with patch("notetaker.preflight.requests.get") as get:
        get.return_value = MagicMock(raise_for_status=MagicMock())
        from notetaker.preflight import check_gemini

        check_gemini(cfg)
    assert "gemini-2.5-flash-lite" in get.call_args.args[0]


def test_preflight_skips_smtp_when_email_is_disabled(monkeypatch, tmp_path, caplog):
    """The wizard's default answer is "no email". That writes the SMTP keys
    commented out, so there is nothing to check — and checking localhost:25
    inside the container is a guaranteed connection refused, which turned
    the default install into a crash loop before 2026-09-14."""
    monkeypatch.setenv("NEXTCLOUD_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_USER", "bot")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SMTP_HOST", raising=False)
    cfg = Config.from_env()

    with (
        patch("notetaker.preflight.check_smtp") as smtp,
        patch("notetaker.preflight.check_nextcloud"),
        patch("notetaker.preflight.check_notes_target"),
        patch("notetaker.preflight.check_gemini"),
        caplog.at_level("INFO", logger="notetaker.preflight"),
    ):
        failures = run_preflight(cfg)

    assert failures == []
    smtp.assert_not_called()
    assert "SMTP_HOST" in caplog.text, "say why the smtp check was skipped"


def test_preflight_still_checks_smtp_when_email_is_enabled(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    assert cfg.email_enabled is True
    with (
        patch("notetaker.preflight.check_smtp") as smtp,
        patch("notetaker.preflight.check_nextcloud"),
        patch("notetaker.preflight.check_notes_target"),
        patch("notetaker.preflight.check_gemini"),
    ):
        run_preflight(cfg)
    smtp.assert_called_once()


# --- whisper ---------------------------------------------------------------


def _whisper_cfg(tmp_path, **kw):
    return make_cfg(state_dir=str(tmp_path), transcription_backend="whisper", **kw)


@contextlib.contextmanager
def other_checks_ok(monkeypatch):
    """Every non-whisper check passes, and the faster_whisper package is
    present (a stub module), so these tests do not depend on the dev venv."""
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())
    with (
        patch("notetaker.preflight.check_smtp"),
        patch("notetaker.preflight.check_nextcloud"),
        patch("notetaker.preflight.check_notes_target"),
        patch("notetaker.preflight.check_gemini"),
    ):
        yield


def test_whisper_check_is_skipped_for_the_gemini_backend(tmp_path, monkeypatch):
    cfg = make_cfg(state_dir=str(tmp_path))
    with (
        other_checks_ok(monkeypatch),
        patch("notetaker.preflight.check_whisper", side_effect=AssertionError("must not run")),
    ):
        assert run_preflight(cfg) == []


def test_missing_model_names_the_download_command_and_the_alternative(tmp_path, monkeypatch):
    cfg = _whisper_cfg(tmp_path)
    with (
        other_checks_ok(monkeypatch),
        patch("notetaker.whisper.model_is_downloaded", return_value=False),
    ):
        failures = run_preflight(cfg)
    assert len(failures) == 1 and failures[0].startswith("whisper:")
    assert "docker compose run --rm --no-deps notetaker download-model" in failures[0]
    assert "TRANSCRIPTION_BACKEND=gemini" in failures[0]
    assert "large-v3-turbo" in failures[0]


def test_unknown_model_name_is_reported_as_such(tmp_path, monkeypatch):
    cfg = _whisper_cfg(tmp_path, whisper_model="typo")
    with (
        other_checks_ok(monkeypatch),
        patch("notetaker.whisper.model_is_downloaded", side_effect=ValueError("Invalid model size")),
    ):
        failures = run_preflight(cfg)
    assert "unknown Whisper model name 'typo'" in failures[0]


def test_present_model_passes(tmp_path, monkeypatch):
    cfg = _whisper_cfg(tmp_path)
    with (
        other_checks_ok(monkeypatch),
        patch("notetaker.whisper.model_is_downloaded", return_value=True),
    ):
        assert run_preflight(cfg) == []


def test_missing_faster_whisper_package_is_a_clear_failure(tmp_path, monkeypatch):
    cfg = _whisper_cfg(tmp_path)
    with other_checks_ok(monkeypatch):
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        failures = run_preflight(cfg)
    assert "faster-whisper is not installed" in failures[0]


# --- whisper: container memory cap ------------------------------------------


def test_whisper_check_fails_under_a_2g_memory_cap(tmp_path, monkeypatch):
    limit_file = tmp_path / "memory.max"
    limit_file.write_text("2147483648")
    cfg = _whisper_cfg(tmp_path)
    with (
        other_checks_ok(monkeypatch),
        patch("notetaker.whisper.model_is_downloaded", return_value=True),
        patch(
            "notetaker.preflight.CGROUP_MEMORY_LIMIT_FILES",
            (str(limit_file), str(tmp_path / "missing")),
        ),
    ):
        failures = run_preflight(cfg)
    assert len(failures) == 1 and failures[0].startswith("whisper:")
    assert "NOTETAKER_MEMORY_LIMIT=6g" in failures[0]
    assert "2.0g" in failures[0]


def test_whisper_check_passes_with_6g_or_no_cap(tmp_path, monkeypatch):
    for content in ("6442450944", "max", None):
        limit_file = tmp_path / "memory.max"
        if content is None:
            if limit_file.exists():
                limit_file.unlink()
        else:
            limit_file.write_text(content)
        cfg = _whisper_cfg(tmp_path)
        with (
            other_checks_ok(monkeypatch),
            patch("notetaker.whisper.model_is_downloaded", return_value=True),
            patch(
                "notetaker.preflight.CGROUP_MEMORY_LIMIT_FILES",
                (str(limit_file), str(tmp_path / "missing")),
            ),
        ):
            assert run_preflight(cfg) == [], content


def test_v1_no_limit_sentinel_means_unlimited(tmp_path):
    v1_file = tmp_path / "memory.limit_in_bytes"
    v1_file.write_text("9223372036854771712")
    with patch(
        "notetaker.preflight.CGROUP_MEMORY_LIMIT_FILES",
        (str(tmp_path / "missing"), str(v1_file)),
    ):
        assert container_memory_limit() is None
