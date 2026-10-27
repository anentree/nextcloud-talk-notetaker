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
        # Rooms already logged as showing a call with nobody in it, so a
        # lingering session is reported once, not every poll.
        self._empty_logged: set[str] = set()

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
            # A lingering session is not a call waiting for the bot.
            and not self._nobody_in_call(r["token"])
            for r in self._get_rooms()
        )

    def check_for_new_calls(self) -> list[dict[str, Any]]:
        rooms = self._get_rooms()

        currently_active = {
            r["token"] for r in rooms if r.get("hasCall") or r.get("callFlag", 0) > 0
        }
        new_calls = currently_active - self._active_tokens
        # A room can look "in a call" with nobody in it: the bot's own session
        # lingers ~30s after it leaves. Joining that is how the bot used to
        # chase the call it had just left, and once the lingering session
        # expired it would start a new call and ring everyone. A call only
        # counts once a person is in it. Not remembered as active meanwhile,
        # so it is looked at again on the next poll, when someone may have
        # joined.
        rung = self._poll_call_notifications() | self._pending_rings
        empty = {
            token
            for token in new_calls
            if token not in rung and self._nobody_in_call(token)
        }
        new_calls -= empty
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
        self._active_tokens = currently_active - empty
        self._empty_logged &= currently_active

        # Rung rooms first: the bot has just left another call for them, and
        # must not be sent straight back to the room it left.
        return sorted(
            (r for r in rooms if r["token"] in new_calls),
            key=lambda r: r["token"] not in rung,
        )

    def _nobody_in_call(self, token: str) -> bool:
        """True when the room's call has no participant but the bot in it.
        Unknown (the check failed) counts as somebody: a missed meeting costs
        more than a pointless join."""
        # Imported here: the recorder pulls in the browser stack.
        from notetaker.recorder import _call_presence

        presence = _call_presence(self.base_url, self.auth, token, self.auth[0])
        if presence is None:
            return False
        if presence[1] > 0:
            self._empty_logged.discard(token)
            return False
        if token not in self._empty_logged:
            self._empty_logged.add(token)
            log.info("Room %s shows a call but nobody is in it; not joining", token)
        return True

    def clear_token(self, token: str) -> None:
        """Remove a token from the active set.

        Call after handle_call finishes so that if the call is still ongoing,
        it will be re-detected on the next poll cycle.
        """
        self._active_tokens.discard(token)
