from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}
MAX_RETRIES = 3
RETRY_DELAY = 5


def _get_email_from_api(base: str, auth: tuple, uid: str) -> str:
    """Try to get email via Nextcloud user API (requires admin)."""
    try:
        user_resp = requests.get(
            f"{base}/ocs/v1.php/cloud/users/{uid}",
            auth=auth,
            headers=OCS_HEADERS,
        )
        user_resp.raise_for_status()
        data = user_resp.json()["ocs"]["data"]
        if isinstance(data, dict):
            return data.get("email", "")
    except Exception as exc:
        log.debug("API email lookup failed for %s: %s", uid, exc)
    return ""


def get_participant_emails(
    nextcloud_url: str,
    user: str,
    password: str,
    room_token: str,
    mail_domain: str = "",
    email_overrides: dict[str, str] | None = None,
    exclude_user: str = "",
) -> list[dict[str, str]]:
    base = nextcloud_url.rstrip("/")
    auth = (user, password)
    overrides = email_overrides or {}
    log.info("Email overrides: %s", overrides)

    participants = []
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{base}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
                auth=auth,
                headers=OCS_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            participants = resp.json()["ocs"]["data"]
            break
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                log.warning(
                    "Failed to get participants (attempt %d/%d): %s. Retrying...",
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                time.sleep(RETRY_DELAY)
    else:
        log.error(
            "Failed to get participants after %d attempts: %s", MAX_RETRIES, last_exc
        )
        return []

    # Determine fallback domain for email derivation
    fallback_domain = mail_domain or urlparse(nextcloud_url).hostname

    result = []
    for p in participants:
        if p["actorType"] != "users":
            continue
        uid = p["actorId"]
        if uid == exclude_user:
            continue

        # Check overrides first, then API, then derive from domain
        if uid in overrides:
            email = overrides[uid]
            log.info("Using override email for user %s: %s", uid, email)
        else:
            email = _get_email_from_api(base, auth, uid)
            if not email:
                email = f"{uid}@{fallback_domain}"
                log.info("Using derived email for user %s: %s", uid, email)

        result.append(
            {
                "user_id": uid,
                "display_name": p["displayName"],
                "email": email,
            }
        )

    return result
