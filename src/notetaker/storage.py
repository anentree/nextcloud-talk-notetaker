from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


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
        resp = requests.request("MKCOL", current, auth=auth)
        if resp.status_code not in (201, 405):
            resp.raise_for_status()
    folder_url = current

    # Upload file
    file_url = f"{folder_url}/{filename}"
    resp = requests.put(
        file_url,
        auth=auth,
        data=content.encode("utf-8"),
        headers={"Content-Type": "text/markdown; charset=utf-8"},
    )
    resp.raise_for_status()

    log.info("Uploaded notes to %s/%s", folder, filename)
    return file_url
