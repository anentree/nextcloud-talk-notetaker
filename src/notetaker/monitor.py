from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


class CallMonitor:
    def __init__(self, nextcloud_url: str, user: str, password: str) -> None:
        self.base_url = nextcloud_url.rstrip("/")
        self.auth = (user, password)
        self._active_tokens: set[str] = set()
        # Notification ids already acted on, so a failed dismissal cannot
        # make the same ring join the same call twice.
        self._seen_rings: set[int] = set()
        # Rings read while a call was being recorded, not yet acted on.
        self._pending_rings: set[str] = set()

    def _get_rooms(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url,
                    auth=self.auth,
                    headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json()["ocs"]["data"]
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Failed to fetch rooms (attempt %d/%d): %s. Retrying in %ds...",
                        attempt,
                        MAX_RETRIES,
                        exc,
                        RETRY_DELAY,
                    )
                    time.sleep(RETRY_DELAY)
        log.error("Failed to fetch rooms after %d attempts: %s", MAX_RETRIES, last_exc)
        return []

    def _poll_call_notifications(self) -> set[str]:
        """Room tokens of "call notification" rings sent to the bot.

        Pressing "Send call notification" in Talk raises a spreed notification
        of object_type "call" whose object_id is the room token. It is the
        user's explicit "join this call now", so it overrides the usual
        already-seen bookkeeping. Best effort: a notifications outage must
        never stop ordinary call detection.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/notifications/api/v2/notifications"
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        try:
            resp = requests.get(url, auth=self.auth, headers=headers, timeout=15)
            resp.raise_for_status()
            # 204 with no body is how Nextcloud says "no notifications".
            if resp.status_code == 204 or not resp.content:
                return set()
            notifications = resp.json()["ocs"]["data"]
        except Exception as exc:
            log.warning("Could not fetch call notifications: %s", exc)
            return set()

        tokens: set[str] = set()
        for n in notifications:
            if n.get("app") != "spreed" or n.get("object_type") != "call":
                continue
            nid = n.get("notification_id")
            if nid in self._seen_rings:
                continue
            self._seen_rings.add(nid)
            tokens.add(str(n.get("object_id")))
            try:
                requests.delete(
                    f"{url}/{nid}", auth=self.auth, headers=headers, timeout=15
                )
            except Exception as exc:
                log.debug("Could not dismiss notification %s: %s", nid, exc)
        return tokens

    def ring_for_other_room(self, current_token: str) -> bool:
        """True if someone rang the bot into a call in a room other than the
        one being recorded. The ring is kept for check_for_new_calls, which
        joins it as soon as the recording is finished."""
        self._pending_rings |= self._poll_call_notifications()
        self._pending_rings.discard(current_token)
        if not self._pending_rings:
            return False
        rooms = self._get_rooms()
        if not rooms:
            # The room list failed (it returns [] after its retries). The ring
            # is already dismissed, so keep it for the next try.
            return False
        active = {
            r["token"] for r in rooms if r.get("hasCall") or r.get("callFlag", 0) > 0
        }
        # A ring for a call that has ended must not pull the bot out of one
        # that is still going.
        self._pending_rings &= active
        return bool(self._pending_rings)

    def other_call_waiting(self, current_token: str) -> bool:
        """True if a call is active in any room other than the one being
        recorded. Read-only: it does not touch the detection bookkeeping."""
        return any(
            r["token"] != current_token
            and (r.get("hasCall") or r.get("callFlag", 0) > 0)
            for r in self._get_rooms()
        )

    def check_for_new_calls(self) -> list[dict[str, Any]]:
        rooms = self._get_rooms()

        currently_active = {
            r["token"] for r in rooms if r.get("hasCall") or r.get("callFlag", 0) > 0
        }
        new_calls = currently_active - self._active_tokens
        rung = self._poll_call_notifications() | self._pending_rings
        self._pending_rings = set()
        for token in rung:
            if token in currently_active:
                log.info("Call notification for room %s: joining", token)
                new_calls.add(token)
            else:
                log.info("Call notification for room %s, but no call is active", token)
        ended_calls = self._active_tokens - currently_active

        for token in ended_calls:
            log.info("Call ended in room %s", token)
        self._active_tokens = currently_active

        # Rung rooms first: the bot has just left another call for them, and
        # must not be sent straight back to the room it left.
        return sorted(
            (r for r in rooms if r["token"] in new_calls),
            key=lambda r: r["token"] not in rung,
        )

    def clear_token(self, token: str) -> None:
        """Remove a token from the active set.

        Call after handle_call finishes so that if the call is still ongoing,
        it will be re-detected on the next poll cycle.
        """
        self._active_tokens.discard(token)
