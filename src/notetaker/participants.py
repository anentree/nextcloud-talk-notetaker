from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}
MAX_RETRIES = 3
RETRY_DELAY = 5


def _get_email_from_api(base: str, auth: tuple, uid: str) -> tuple[str, bool]:
    """Look up a user's email via the Nextcloud user API (requires admin).

    Returns (email, lookup_ok). ``lookup_ok`` distinguishes "the user
    genuinely has no email on file" from "the lookup itself failed" —
    collapsing those two into an empty string is how bogus addresses get
    silently fabricated.
    """
    try:
        user_resp = requests.get(
            f"{base}/ocs/v1.php/cloud/users/{uid}",
            auth=auth,
            headers=OCS_HEADERS,
            timeout=15,
        )
        user_resp.raise_for_status()
        data = user_resp.json()["ocs"]["data"]
        if isinstance(data, dict):
            return data.get("email", "") or "", True
        return "", True
    except Exception as exc:
        log.warning("API email lookup failed for %s: %s", uid, exc)
    return "", False


def get_participant_emails(
    nextcloud_url: str,
    user: str,
    password: str,
    room_token: str,
    mail_domain: str = "",
    email_overrides: dict[str, str] | None = None,
    exclude_user: str = "",
    attendee_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Resolve room participants to email addresses.

    ``attendee_ids`` — when provided, only these user IDs are returned.
    The Talk participants endpoint lists every *member of the conversation*,
    and once a call has ended everyone's ``inCall`` flag is back to 0, so
    attendance cannot be recovered after the fact. The recorder observes it
    live during the call and passes it in here. Without this filter, a
    two-person call inside a twelve-member room emails the notes to ten
    people who were never on it.
    """
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
    if not fallback_domain:
        log.error(
            "No MAIL_DOMAIN and NEXTCLOUD_URL has no hostname (%s) — cannot derive "
            "fallback addresses",
            nextcloud_url,
        )

    result = []
    skipped_non_attendee = []
    skipped_guest = 0
    for p in participants:
        if p.get("actorType") != "users":
            skipped_guest += 1
            continue
        uid = p.get("actorId")
        if not uid or uid == exclude_user:
            continue

        # Only people who were actually on the call, when we know who they were.
        if attendee_ids is not None and uid not in attendee_ids:
            skipped_non_attendee.append(uid)
            continue

        # Check overrides first, then API, then derive from domain
        if uid in overrides:
            email = overrides[uid]
            log.info("Using override email for user %s: %s", uid, email)
        else:
            email, lookup_ok = _get_email_from_api(base, auth, uid)
            if not email:
                if not fallback_domain:
                    log.error("No email for user %s and no fallback domain — skipping", uid)
                    continue
                email = f"{uid}@{fallback_domain}"
                if lookup_ok:
                    log.info("Using derived email for user %s: %s", uid, email)
                else:
                    # The lookup errored, so this address is a guess that may
                    # bounce or reach the wrong person. Say so loudly.
                    log.warning(
                        "Email lookup FAILED for user %s — guessing %s. Verify this "
                        "address; set EMAIL_OVERRIDES if it is wrong.",
                        uid,
                        email,
                    )

        result.append(
            {
                "user_id": uid,
                "display_name": p.get("displayName") or uid,
                "email": email,
            }
        )

    if skipped_non_attendee:
        log.info(
            "Excluded %d room member(s) who were not in the call: %s",
            len(skipped_non_attendee),
            skipped_non_attendee,
        )
    if skipped_guest:
        log.warning(
            "Skipped %d non-user participant(s) (guests/federated) — they have no "
            "resolvable email and will not receive the notes",
            skipped_guest,
        )
    return result
