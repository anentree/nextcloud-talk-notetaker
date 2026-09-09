"""Recipients must be the people who were on the call, not room members.

A Talk conversation can have many members who never joined a given call.
Once the call ends every participant's inCall flag resets to 0, so
attendance is only observable while the call is running.
"""

from __future__ import annotations

import responses

from notetaker.participants import get_participant_emails

NEXTCLOUD_URL = "https://nc.example.com"
PARTICIPANTS_ENDPOINT = (
    f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/abc123/participants"
)

ROOM_MEMBERS = {
    "ocs": {
        "data": [
            {"actorType": "users", "actorId": "bot", "displayName": "AI Notetaker"},
            {"actorType": "users", "actorId": "alice", "displayName": "Alice"},
            {"actorType": "users", "actorId": "bob", "displayName": "Bob"},
            {"actorType": "users", "actorId": "carol", "displayName": "Carol"},
        ]
    }
}


def _register_room():
    responses.get(PARTICIPANTS_ENDPOINT, json=ROOM_MEMBERS, status=200)


def _register_emails():
    for uid in ("alice", "bob", "carol"):
        responses.get(
            f"{NEXTCLOUD_URL}/ocs/v1.php/cloud/users/{uid}",
            json={"ocs": {"data": {"email": f"{uid}@example.com"}}},
            status=200,
        )


@responses.activate
def test_only_attendees_receive_the_notes():
    _register_room()
    _register_emails()

    result = get_participant_emails(
        NEXTCLOUD_URL,
        "bot",
        "secret",
        "abc123",
        exclude_user="bot",
        attendee_ids={"alice"},
    )

    assert [p["user_id"] for p in result] == ["alice"]


@responses.activate
def test_no_attendee_info_falls_back_to_room_membership():
    """Backwards-compatible: when attendance is unknown, behave as before
    rather than emailing nobody."""
    _register_room()
    _register_emails()

    result = get_participant_emails(
        NEXTCLOUD_URL, "bot", "secret", "abc123", exclude_user="bot"
    )

    assert sorted(p["user_id"] for p in result) == ["alice", "bob", "carol"]


@responses.activate
def test_bot_is_never_a_recipient_even_if_marked_as_attendee():
    _register_room()
    _register_emails()

    result = get_participant_emails(
        NEXTCLOUD_URL,
        "bot",
        "secret",
        "abc123",
        exclude_user="bot",
        attendee_ids={"bot", "alice"},
    )

    assert [p["user_id"] for p in result] == ["alice"]


@responses.activate
def test_failed_lookup_is_distinguishable_from_no_email(caplog):
    """A lookup error used to be indistinguishable from "user has no email",
    so a transient 500 silently produced a guessed address logged as normal."""
    _register_room()
    responses.get(f"{NEXTCLOUD_URL}/ocs/v1.php/cloud/users/alice", status=500)

    with caplog.at_level("WARNING"):
        result = get_participant_emails(
            NEXTCLOUD_URL,
            "bot",
            "secret",
            "abc123",
            mail_domain="example.com",
            exclude_user="bot",
            attendee_ids={"alice"},
        )

    assert result[0]["email"] == "alice@example.com"
    assert any("lookup FAILED" in r.message for r in caplog.records)


@responses.activate
def test_email_override_is_used():
    """Production depends on EMAIL_OVERRIDES and it had no test coverage."""
    _register_room()

    result = get_participant_emails(
        NEXTCLOUD_URL,
        "bot",
        "secret",
        "abc123",
        email_overrides={"alice": "alice@example.com"},
        exclude_user="bot",
        attendee_ids={"alice"},
    )

    assert result[0]["email"] == "alice@example.com"


@responses.activate
def test_guests_are_skipped_with_a_warning(caplog):
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    {"actorType": "guests", "actorId": "g1", "displayName": "Guest"},
                ]
            }
        },
        status=200,
    )

    with caplog.at_level("WARNING"):
        result = get_participant_emails(
            NEXTCLOUD_URL, "bot", "secret", "abc123", exclude_user="bot"
        )

    assert result == []
    assert any("guests/federated" in r.message for r in caplog.records)
