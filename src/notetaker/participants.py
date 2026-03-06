from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}


def get_participant_emails(
    nextcloud_url: str,
    user: str,
    password: str,
    room_token: str,
    exclude_user: str = "",
) -> list[dict[str, str]]:
    base = nextcloud_url.rstrip("/")
    auth = (user, password)

    resp = requests.get(
        f"{base}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
        auth=auth,
        headers=OCS_HEADERS,
    )
    resp.raise_for_status()
    participants = resp.json()["ocs"]["data"]

    result = []
    for p in participants:
        if p["actorType"] != "users":
            continue
        uid = p["actorId"]
        if uid == exclude_user:
            continue

        user_resp = requests.get(
            f"{base}/ocs/v1.php/cloud/users/{uid}",
            auth=auth,
            headers=OCS_HEADERS,
        )
        user_resp.raise_for_status()
        email = user_resp.json()["ocs"]["data"].get("email", "")

        if not email:
            log.warning("No email for user %s, skipping", uid)
            continue

        result.append(
            {
                "user_id": uid,
                "display_name": p["displayName"],
                "email": email,
            }
        )

    return result
