import responses
from notetaker.storage import upload_notes


NEXTCLOUD_URL = "https://nc.example.com"


@responses.activate
def test_upload_notes_creates_folder_and_file():
    user = "ai-notetaker"
    folder = "/Talk/Notes"
    filename = "2026-03-06-standup.md"
    notes_content = "# Meeting: Standup\n\n## Summary\nStuff happened."

    # MKCOL to create folder (may already exist -- 405 is OK)
    responses.add(
        "MKCOL",
        f"{NEXTCLOUD_URL}/remote.php/dav/files/{user}{folder}",
        status=405,
    )

    # PUT to upload file
    responses.add(
        responses.PUT,
        f"{NEXTCLOUD_URL}/remote.php/dav/files/{user}{folder}/{filename}",
        status=201,
    )

    upload_notes(NEXTCLOUD_URL, user, "secret", folder, filename, notes_content)

    assert len(responses.calls) == 2
    put_call = responses.calls[1]
    assert put_call.request.body == notes_content.encode()
