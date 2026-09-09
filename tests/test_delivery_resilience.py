"""Regression tests for the 2026-08-13 silent email-loss incident.

A 79-minute call was recorded, transcribed and uploaded, then the SMTP send
timed out against a stale host. The exception was swallowed, the audio was
deleted, and nobody was told. Every test here pins one link in that chain.
"""

from __future__ import annotations

import os
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from notetaker import spool
from notetaker.config import Config
from notetaker.mailer import build_notes_message, send_notes_email
from notetaker.main import deliver_notes, drain_spool


def make_cfg(**kw) -> Config:
    base = dict(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_from="bot@example.com",
    )
    base.update(kw)
    return Config(**base)


ENVELOPE = {
    "room": "abc123",
    "room_name": "Standup",
    "recipients": [{"user_id": "alice", "display_name": "Alice", "email": "a@x.com"}],
    "subject": "Meeting Notes",
    "body": "hello",
    "notes": "# Meeting: Standup",
    "filename": "notes.md",
}


# --- the actual production failure -----------------------------------------


def test_smtp_is_called_with_a_timeout():
    """No timeout means a dead host blocks the poll loop for minutes, or forever.

    The incident took 141 seconds per attempt on TCP connect alone; a host
    that accepts the connection but never speaks would have hung indefinitely.
    """
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = False
        smtp.send_message.return_value = {}

        send_notes_email(
            smtp_host="mail.example.com",
            smtp_port=587,
            smtp_from="bot@example.com",
            smtp_user="",
            smtp_password="",
            recipients=[{"email": "a@x.com"}],
            subject="s",
            body="b",
            notes_markdown="n",
            timeout=17,
        )

    assert smtp_cls.call_args.kwargs["timeout"] == 17


def test_partial_refusal_returns_only_accepted_addresses():
    """smtplib only raises when ALL recipients are refused; a partial refusal
    means the message WAS delivered to everyone else.

    Raising here would make the caller retry the whole envelope and send
    duplicates to the people who already received it.
    """
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = False
        smtp.send_message.return_value = {"bob@x.com": (550, b"No such user")}

        accepted = send_notes_email(
            smtp_host="mail.example.com",
            smtp_port=587,
            smtp_from="bot@example.com",
            smtp_user="",
            smtp_password="",
            recipients=[{"email": "a@x.com"}, {"email": "bob@x.com"}],
            subject="s",
            body="b",
            notes_markdown="n",
        )

    assert accepted == ["a@x.com"]


def test_starttls_is_not_gated_on_credentials():
    """TLS is a property of the transport, not of whether creds are configured."""
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = True
        smtp.send_message.return_value = {}

        send_notes_email(
            smtp_host="mail.example.com",
            smtp_port=587,
            smtp_from="bot@example.com",
            smtp_user="",
            smtp_password="",
            recipients=[{"email": "a@x.com"}],
            subject="s",
            body="b",
            notes_markdown="n",
        )

    smtp.starttls.assert_called_once()
    smtp.login.assert_not_called()


def _smtp_without_starttls(smtp_cls):
    smtp = smtp_cls.return_value
    smtp.__enter__.return_value = smtp
    smtp.has_extn.return_value = False
    smtp.send_message.return_value = {}
    return smtp


def _send(host, user="bot", password="pw"):
    return send_notes_email(
        smtp_host=host,
        smtp_port=587,
        smtp_from="bot@example.com",
        smtp_user=user,
        smtp_password=password,
        recipients=[{"email": "a@x.com"}],
        subject="s",
        body="b",
        notes_markdown="n",
    )


def test_credentials_are_never_sent_in_cleartext_to_a_remote_host():
    """Regression: when the server stopped advertising STARTTLS the new code
    skipped the upgrade and went straight to login(), so a Postfix cert-renewal
    failure would leak SMTP credentials with no exception and no log line.
    The old unconditional starttls() failed loudly; this restores that."""
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = _smtp_without_starttls(smtp_cls)
        with pytest.raises(smtplib.SMTPNotSupportedError, match="cleartext"):
            _send("mail.example.com")
    smtp.starttls.assert_not_called()
    smtp.login.assert_not_called()
    smtp.send_message.assert_not_called()


def test_cleartext_credentials_are_tolerated_on_the_same_host(caplog):
    """The docker bridge never leaves the machine; a Postfix that offers no
    STARTTLS there gets a loud warning, not a refusal that takes email down."""
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = _smtp_without_starttls(smtp_cls)
        with caplog.at_level("WARNING", logger="notetaker.mailer"):
            _send("host.docker.internal")
    smtp.starttls.assert_not_called()
    smtp.login.assert_called_once()
    smtp.send_message.assert_called_once()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and "STARTTLS" in warnings[0].getMessage()


def test_cleartext_credentials_are_refused_to_a_lan_address():
    """A private address still crosses a wire an attacker can sit on. It
    skips *certificate* checks under "auto" (it cannot carry a public cert)
    but it is not a safe place for cleartext AUTH — refuse, like a remote host."""
    for host in ("192.168.1.10", "10.0.0.5", "100.64.0.1", "mail.internal"):
        with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
            smtp = _smtp_without_starttls(smtp_cls)
            with pytest.raises(smtplib.SMTPNotSupportedError):
                _send(host)
        assert not smtp.login.called, host
        assert not smtp.send_message.called, host


def test_no_starttls_without_credentials_is_not_an_error():
    """Nothing secret is on the wire, so an unauthenticated plaintext relay
    keeps working."""
    with patch("notetaker.mailer.smtplib.SMTP") as smtp_cls:
        smtp = _smtp_without_starttls(smtp_cls)
        _send("mail.example.com", user="", password="")
    smtp.starttls.assert_not_called()
    smtp.login.assert_not_called()
    smtp.send_message.assert_called_once()


def test_recipient_addresses_never_appear_in_headers():
    """Attendees must not see each other's addresses, and the sender must not
    become an extra envelope recipient."""
    msg = build_notes_message(
        "bot@example.com",
        [{"email": "a@x.com"}, {"email": "b@x.com"}],
        "subject",
        "body",
        "# notes",
    )
    rendered = str(msg)
    assert "a@x.com" not in rendered
    assert "b@x.com" not in rendered
    assert msg["Bcc"] is None
    assert msg["To"] == "Undisclosed recipients:;"


def test_header_newlines_are_stripped():
    """A display name or override containing a newline must not blow up the send."""
    msg = build_notes_message(
        "bot@example.com",
        [{"email": "a@x.com"}],
        "Subject\r\nBcc: attacker@evil.com",
        "body",
        "# notes",
    )
    assert "\n" not in msg["Subject"]
    assert msg["Bcc"] is None


# --- retry + spool ----------------------------------------------------------


def test_failed_send_is_spooled_not_lost(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    with (
        patch("notetaker.main._send_envelope", side_effect=TimeoutError("timed out")),
        patch("notetaker.main.time.sleep"),
    ):
        assert deliver_notes(cfg, ENVELOPE) is False

    queued = spool.pending(cfg.spool_dir)
    assert len(queued) == 1, "a failed email must be persisted for retry"


def test_send_is_retried_before_spooling(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    attempts = {"n": 0}

    def flaky(_cfg, _env):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("transient")
        return {}

    with patch("notetaker.main._send_envelope", flaky), patch("notetaker.main.time.sleep"):
        assert deliver_notes(cfg, ENVELOPE) is True

    assert attempts["n"] == 3
    assert spool.pending(cfg.spool_dir) == []


def test_retry_never_resends_to_someone_already_delivered(tmp_path):
    """The duplicate-delivery trap: a partial refusal must narrow the retry
    to the refused addresses only, or the recipients who succeeded receive a
    fresh copy on every one of the retry attempts."""
    cfg = make_cfg(state_dir=str(tmp_path))
    envelope = dict(
        ENVELOPE,
        recipients=[
            {"user_id": "alice", "display_name": "Alice", "email": "a@x.com"},
            {"user_id": "bob", "display_name": "Bob", "email": "bad@x.com"},
        ],
    )
    seen: list[list[str]] = []

    def refuse_bob(cfg_, env):
        addrs = [r["email"] for r in env["recipients"]]
        seen.append(addrs)
        remaining = [r for r in env["recipients"] if r["email"] != "a@x.com"]
        return dict(env, recipients=remaining) if remaining else {}

    with patch("notetaker.main._send_envelope", refuse_bob), patch("notetaker.main.time.sleep"):
        assert deliver_notes(cfg, envelope) is False

    assert seen[0] == ["a@x.com", "bad@x.com"]
    for subsequent in seen[1:]:
        assert "a@x.com" not in subsequent, (
            "alice already received the notes; retrying her address duplicates it"
        )

    # Only the still-undelivered recipient is queued.
    import json

    with open(spool.pending(cfg.spool_dir)[0]) as f:
        queued = json.load(f)
    assert [r["email"] for r in queued["recipients"]] == ["bad@x.com"]


def test_spool_drains_when_mail_comes_back(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    spool.enqueue(cfg.spool_dir, ENVELOPE)
    assert len(spool.pending(cfg.spool_dir)) == 1

    with patch("notetaker.main._send_envelope", return_value={}) as send:
        drain_spool(cfg)

    send.assert_called_once()
    assert spool.pending(cfg.spool_dir) == [], "delivered mail must leave the spool"


def test_spool_keeps_envelope_when_retry_fails(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    spool.enqueue(cfg.spool_dir, ENVELOPE)

    with patch("notetaker.main._send_envelope", side_effect=OSError("still down")):
        drain_spool(cfg)

    remaining = spool.pending(cfg.spool_dir)
    assert len(remaining) == 1
    import json

    with open(remaining[0]) as f:
        env = json.load(f)
    assert env["attempts"] == 1
    assert env["next_attempt_at"] > 0, "a retry must be scheduled, not immediate"


def test_spool_backs_off_instead_of_hammering(tmp_path):
    """Retries used to run every 10s, burning the whole budget in ~2 minutes.
    The outage this exists for lasted months."""
    cfg = make_cfg(state_dir=str(tmp_path))
    path = spool.enqueue(cfg.spool_dir, ENVELOPE)
    calls = {"n": 0}

    def always_fail(_env):
        calls["n"] += 1
        raise OSError("down")

    now = 1_000_000.0
    spool.drain(cfg.spool_dir, always_fail, now=now)
    assert calls["n"] == 1

    # Immediately after, the envelope is not yet due.
    spool.drain(cfg.spool_dir, always_fail, now=now + 1)
    assert calls["n"] == 1, "envelope retried before its backoff elapsed"

    # After the backoff window it is retried again.
    spool.drain(cfg.spool_dir, always_fail, now=now + spool.INITIAL_BACKOFF_SECONDS + 1)
    assert calls["n"] == 2
    assert spool.pending(cfg.spool_dir) == [path]


def test_spool_gives_up_on_age_not_attempt_count(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    now = 1_000_000.0
    path = spool.enqueue(cfg.spool_dir, dict(ENVELOPE, created_at=now))

    # Still young: kept, even after many attempts.
    spool.drain(
        cfg.spool_dir,
        MagicMock(side_effect=OSError("down")),
        now=now + spool.MAX_AGE_SECONDS - 10,
    )
    assert spool.pending(cfg.spool_dir) == [path]

    # Past the window: retired to .failed, notes still on disk.
    spool.drain(
        cfg.spool_dir,
        MagicMock(side_effect=OSError("down")),
        now=now + spool.MAX_AGE_SECONDS + 10,
    )
    assert spool.pending(cfg.spool_dir) == []
    assert os.path.exists(path + ".failed"), "notes must remain recoverable on disk"


def test_spool_survives_unreadable_envelope(tmp_path):
    cfg = make_cfg(state_dir=str(tmp_path))
    os.makedirs(cfg.spool_dir, exist_ok=True)
    with open(os.path.join(cfg.spool_dir, "broken.json"), "w") as f:
        f.write("{not json")
    spool.enqueue(cfg.spool_dir, ENVELOPE)

    with patch("notetaker.main._send_envelope", return_value={}) as send:
        drain_spool(cfg)

    send.assert_called_once(), "one bad file must not block the rest of the queue"
