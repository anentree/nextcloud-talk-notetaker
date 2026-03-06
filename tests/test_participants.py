import responses
from notetaker.participants import get_participant_emails


NEXTCLOUD_URL = "https://nc.example.com"


@responses.activate
def test_get_participant_emails():
    token = "abc123"
    participants_url = (
        f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants"
    )
    responses.get(
        participants_url,
        json={
            "ocs": {
                "data": [
                    {"actorType": "users", "actorId": "alice", "displayName": "Alice"},
                    {"actorType": "users", "actorId": "bob", "displayName": "Bob"},
                    {
                        "actorType": "users",
                        "actorId": "ai-notetaker",
                        "displayName": "AI Notetaker",
                    },
                    {
                        "actorType": "guests",
                        "actorId": "guest1",
                        "displayName": "Guest",
                    },
                ]
            }
        },
        status=200,
    )

    for uid, email in [
        ("alice", "alice@example.com"),
        ("bob", "bob@example.com"),
        ("ai-notetaker", ""),
    ]:
        responses.get(
            f"{NEXTCLOUD_URL}/ocs/v1.php/cloud/users/{uid}",
            json={"ocs": {"data": {"email": email, "displayname": uid.title()}}},
            status=200,
        )

    result = get_participant_emails(
        NEXTCLOUD_URL, "bot", "secret", token, exclude_user="ai-notetaker"
    )

    assert result == [
        {"user_id": "alice", "display_name": "Alice", "email": "alice@example.com"},
        {"user_id": "bob", "display_name": "Bob", "email": "bob@example.com"},
    ]


@responses.activate
def test_skips_users_without_email():
    token = "abc123"
    participants_url = (
        f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants"
    )
    responses.get(
        participants_url,
        json={
            "ocs": {
                "data": [
                    {
                        "actorType": "users",
                        "actorId": "noemail",
                        "displayName": "No Email",
                    },
                ]
            }
        },
        status=200,
    )
    responses.get(
        f"{NEXTCLOUD_URL}/ocs/v1.php/cloud/users/noemail",
        json={"ocs": {"data": {"email": "", "displayname": "No Email"}}},
        status=200,
    )

    result = get_participant_emails(NEXTCLOUD_URL, "bot", "secret", token)

    assert result == []
