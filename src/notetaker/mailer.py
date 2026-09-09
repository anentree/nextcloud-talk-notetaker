from __future__ import annotations

import ipaddress
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)

# Connect/read timeout for every SMTP operation. Without this, smtplib
# inherits the OS default (no read timeout at all), so a dead or silent
# mail host blocks the single-threaded poll loop indefinitely and the
# service stops noticing calls entirely.
DEFAULT_SMTP_TIMEOUT = 30

# Accepted spellings for SMTP_TLS_VERIFY. Anything outside these sets is
# treated as "verify" — an unrecognised value must never turn verification
# off by accident.
TLS_VERIFY_TRUE = frozenset({"true", "1", "yes", "on"})
TLS_VERIFY_FALSE = frozenset({"false", "0", "no", "off"})
TLS_VERIFY_AUTO = "auto"


def extract_follow_up_email(notes: str) -> tuple[str, str]:
    """Extract the Follow-Up Email section from the notes.

    Returns (subject, body). Falls back to using the full summary if
    no Follow-Up Email section is found.
    """
    match = re.search(
        r"## Follow-Up Email\s*\n(.*)",
        notes,
        re.DOTALL,
    )
    if not match:
        title_match = re.search(r"# Meeting:\s*(.+)", notes)
        title = title_match.group(1).strip() if title_match else "Meeting"
        return f"Meeting Notes -- {title}", notes

    email_text = match.group(1).strip()

    subject_match = re.search(r"Subject:\s*(.+)", email_text)
    subject = subject_match.group(1).strip() if subject_match else "Meeting Notes"

    if subject_match:
        body = email_text[subject_match.end() :].strip()
    else:
        body = email_text

    return subject, body


def _sanitize_header(value: str) -> str:
    """Strip CR/LF so a stray newline can't inject headers or raise at set time."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def _is_local_target(host: str) -> bool:
    """True for addresses that cannot have a matching public certificate.

    The container reaches Postfix over the docker bridge as
    ``host.docker.internal``; the server presents its real
    ``mail.example.com`` certificate, so hostname verification can
    never succeed on that hop no matter how the mail server is configured.
    """
    host = (host or "").strip().lower().strip("[]")
    if host in ("localhost", "host.docker.internal", "::1"):
        return True
    if host.endswith((".internal", ".local", ".localdomain")):
        return True
    # IP literal: local only when it is NOT globally routable — loopback,
    # RFC 1918, link-local, CGNAT/Tailscale (100.64/10), ULA. A public IP
    # literal is a remote host and gets verified like any other hostname.
    # (ipaddress rejects names such as "cafe", so hex-only hostnames are safe.)
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _is_same_host(host: str) -> bool:
    """True only for hops that never leave this machine: loopback and the
    docker bridge alias. Deliberately narrower than ``_is_local_target`` —
    RFC 1918 and CGNAT addresses skip *certificate* checks under "auto"
    because they cannot carry a public cert, but they do cross a network, so
    they are not a safe place to send credentials in the clear."""
    host = (host or "").strip().lower().strip("[]")
    if host in ("localhost", "host.docker.internal", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _tls_context(smtp_host: str, verify: str = "auto") -> ssl.SSLContext:
    """Build the TLS context.

    ``verify`` is "auto" (default), "true" or "false". Under "auto",
    certificate verification is enabled for real hostnames and disabled for
    local targets, where it cannot succeed.

    This is deliberately not "always verify": turning verification on for
    ``host.docker.internal`` makes every send — and the startup preflight —
    fail with a hostname mismatch, which would take the whole service down.
    """
    ctx = ssl.create_default_context()
    verify = (verify or TLS_VERIFY_AUTO).strip().lower()

    if verify in TLS_VERIFY_TRUE:
        return ctx
    if verify == TLS_VERIFY_AUTO:
        if not _is_local_target(smtp_host):
            return ctx
        log.info(
            "SMTP host %s is a local target — TLS certificate verification "
            "disabled for this hop (it cannot match a public certificate). "
            "Set SMTP_TLS_VERIFY=true to force verification.",
            smtp_host,
        )
    elif verify in TLS_VERIFY_FALSE:
        log.warning(
            "SMTP_TLS_VERIFY=false — TLS certificate verification disabled for %s",
            smtp_host,
        )
    else:
        # Unrecognised value: fail closed. Verify, and say why.
        log.warning(
            "SMTP_TLS_VERIFY=%r is not one of auto/true/false — verifying the "
            "certificate anyway. Set it to false to disable verification on purpose.",
            verify,
        )
        return ctx
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def upgrade_to_tls(
    smtp: smtplib.SMTP,
    smtp_host: str,
    smtp_port: int,
    context: ssl.SSLContext,
    *,
    has_credentials: bool,
) -> None:
    """Upgrade an SMTP session with STARTTLS, or refuse to proceed in the clear.

    Port 465 is implicit TLS and needs nothing. Otherwise STARTTLS is used
    whenever the server offers it — TLS is a property of the transport, not
    of whether credentials happen to be configured.

    If the server offers no STARTTLS and we hold credentials, they must not
    go out in cleartext to anything beyond this machine: that is a silent
    regression from the old unconditional ``starttls()`` call, which failed
    loudly. Only loopback and the docker bridge (``_is_same_host``) get a
    loud warning instead of a refusal — a LAN address crosses a wire an
    attacker can sit on, so it is refused like any remote host.
    """
    if smtp_port == 465:
        return
    if smtp.has_extn("starttls"):
        smtp.starttls(context=context)
        smtp.ehlo()
        return
    if not has_credentials:
        return
    if _is_same_host(smtp_host):
        log.warning(
            "SMTP server %s:%d does not offer STARTTLS — credentials will be "
            "sent unencrypted over a same-host hop",
            smtp_host,
            smtp_port,
        )
        return
    raise smtplib.SMTPNotSupportedError(
        f"SMTP server {smtp_host}:{smtp_port} does not offer STARTTLS; "
        "refusing to send credentials in cleartext beyond this host"
    )


def build_notes_message(
    smtp_from: str,
    recipients: list[dict[str, str]],
    subject: str,
    body: str,
    notes_markdown: str,
    attachment_filename: str = "meeting-notes.md",
) -> EmailMessage:
    """Build the notes email.

    Recipients go in Bcc so attendees don't see each other's addresses.
    """
    msg = EmailMessage()
    msg["Subject"] = _sanitize_header(subject)
    msg["From"] = _sanitize_header(smtp_from)
    # Recipients are supplied to sendmail as an explicit envelope list rather
    # than through To/Cc/Bcc headers. That keeps attendees from seeing each
    # other's addresses without adding the sender as an extra RCPT (which a
    # To: <self> header would).
    msg["To"] = "Undisclosed recipients:;"
    msg.set_content(body)

    msg.add_attachment(
        notes_markdown.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=attachment_filename,
    )
    return msg


def send_notes_email(
    smtp_host: str,
    smtp_port: int,
    smtp_from: str,
    smtp_user: str,
    smtp_password: str,
    recipients: list[dict[str, str]],
    subject: str,
    body: str,
    notes_markdown: str,
    attachment_filename: str = "meeting-notes.md",
    timeout: int = DEFAULT_SMTP_TIMEOUT,
    tls_verify: str = "auto",
) -> list[str]:
    """Send the notes email. Returns the list of ACCEPTED recipient addresses.

    Raises on connection or auth failure. A *partial* recipient refusal does
    NOT raise: the message has already been delivered to everyone else, so
    raising would make the caller retry the whole envelope and send duplicates
    to the people who received it. The accepted list is returned instead, and
    the caller decides what to do about the difference.
    """
    addresses = [r["email"] for r in recipients if r.get("email")]
    if not addresses:
        raise ValueError("No recipient addresses to send to")

    msg = build_notes_message(
        smtp_from, recipients, subject, body, notes_markdown, attachment_filename
    )
    context = _tls_context(smtp_host, tls_verify)

    if smtp_port == 465:
        smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)

    with smtp:
        smtp.ehlo()
        upgrade_to_tls(
            smtp,
            smtp_host,
            smtp_port,
            context,
            has_credentials=bool(smtp_user and smtp_password),
        )
        if smtp_user and smtp_password:
            smtp.login(smtp_user, smtp_password)
        elif smtp_user or smtp_password:
            log.warning(
                "Only one of SMTP_USER/SMTP_PASSWORD is set — sending unauthenticated"
            )
        # Explicit envelope recipients; the message carries no To/Cc/Bcc
        # addresses of its own.
        refused = smtp.send_message(msg, from_addr=smtp_from, to_addrs=addresses) or {}

    accepted = [a for a in addresses if a not in refused]
    if refused:
        log.error(
            "SMTP refused %d of %d recipient(s): %s. Delivered to: %s",
            len(refused),
            len(addresses),
            {k: str(v) for k, v in refused.items()},
            accepted,
        )
    else:
        log.info("Sent notes email to %s", accepted)
    return accepted
