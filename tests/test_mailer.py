from unittest.mock import patch, MagicMock
from notetaker.mailer import send_notes_email, extract_follow_up_email


def test_extract_follow_up_email():
    notes = """# Meeting: Standup

## Summary
We talked about stuff.

## Follow-Up Email

Subject: Meeting Notes -- Standup -- 2026-03-06

Hi everyone,

Here's what we discussed today.

**Action items:**
- Alice: Do the thing

Please let me know if I missed anything.

Best,
AI Notetaker
"""
    subject, body = extract_follow_up_email(notes)

    assert subject == "Meeting Notes -- Standup -- 2026-03-06"
    assert "Here's what we discussed today." in body
    assert "Alice: Do the thing" in body


def test_extract_follow_up_email_fallback():
    notes = "# Meeting: Standup\n\n## Summary\nNo follow-up section here."

    subject, body = extract_follow_up_email(notes)

    assert "Standup" in subject
    assert "No follow-up section here." in body


@patch("notetaker.mailer.smtplib.SMTP")
def test_send_notes_email(mock_smtp_class):
    mock_smtp = MagicMock()
    mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

    recipients = [
        {"email": "alice@example.com", "display_name": "Alice"},
        {"email": "bob@example.com", "display_name": "Bob"},
    ]

    send_notes_email(
        smtp_host="localhost",
        smtp_port=25,
        smtp_from="bot@example.com",
        smtp_user="",
        smtp_password="",
        recipients=recipients,
        subject="Meeting Notes -- Standup",
        body="Hi everyone, here's a summary.",
        notes_markdown="# Full notes here",
    )

    mock_smtp.send_message.assert_called_once()
