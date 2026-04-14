from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 5


def upload_notes(
    nextcloud_url: str,
    user: str,
    password: str,
    folder: str,
    filename: str,
    content: str,
) -> str:
    base = nextcloud_url.rstrip("/")
    auth = (user, password)
    dav_base = f"{base}/remote.php/dav/files/{user}"

    # Ensure folder exists by creating each path segment (MKCOL doesn't
    # create intermediate directories). 405 means already exists, that's fine.
    parts = [p for p in folder.split("/") if p]
    current = dav_base
    for part in parts:
        current = f"{current}/{part}"
        resp = requests.request("MKCOL", current, auth=auth, timeout=15)
        if resp.status_code not in (201, 405):
            resp.raise_for_status()
    folder_url = current

    # Upload file with retry
    file_url = f"{folder_url}/{filename}"
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.put(
                file_url,
                auth=auth,
                data=content.encode("utf-8"),
                headers={"Content-Type": "text/markdown; charset=utf-8"},
                timeout=30,
            )
            resp.raise_for_status()
            log.info("Uploaded notes to %s/%s", folder, filename)
            return file_url
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                log.warning(
                    "Upload failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
    raise RuntimeError(
        f"Failed to upload notes after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc
