from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


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
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(r["email"] for r in recipients)
    msg.set_content(body)

    msg.add_attachment(
        notes_markdown.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=attachment_filename,
    )

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as smtp:
            if smtp_user and smtp_password:
                smtp.starttls()
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)

    log.info("Sent notes email to %s", [r["email"] for r in recipients])
