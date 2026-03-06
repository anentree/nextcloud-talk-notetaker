from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class CallMonitor:
    def __init__(self, nextcloud_url: str, user: str, password: str) -> None:
        self.base_url = nextcloud_url.rstrip("/")
        self.auth = (user, password)
        self._active_tokens: set[str] = set()

    def _get_rooms(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"
        resp = requests.get(
            url,
            auth=self.auth,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["ocs"]["data"]

    def check_for_new_calls(self) -> list[dict[str, Any]]:
        rooms = self._get_rooms()

        currently_active = {
            r["token"] for r in rooms if r.get("hasCall") or r.get("callFlag", 0) > 0
        }
        new_calls = currently_active - self._active_tokens
        ended_calls = self._active_tokens - currently_active

        for token in ended_calls:
            log.info("Call ended in room %s", token)
        self._active_tokens = currently_active

        return [r for r in rooms if r["token"] in new_calls]
