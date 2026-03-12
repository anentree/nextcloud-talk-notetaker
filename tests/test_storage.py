import responses
from notetaker.storage import upload_notes


NEXTCLOUD_URL = "https://nc.example.com"


@responses.activate
def test_upload_notes_creates_folder_and_file():
    user = "ai-notetaker"
    folder = "/Talk/Notes"
    filename = "2026-03-06-standup.md"
    notes_content = "# Meeting: Standup\n\n## Summary\nStuff happened."
    dav_base = f"{NEXTCLOUD_URL}/remote.php/dav/files/{user}"

    # MKCOL for each path segment (recursive creation)
    responses.add("MKCOL", f"{dav_base}/Talk", status=405)
    responses.add("MKCOL", f"{dav_base}/Talk/Notes", status=405)

    # PUT to upload file
    responses.add(
        responses.PUT,
        f"{dav_base}/Talk/Notes/{filename}",
        status=201,
    )

    upload_notes(NEXTCLOUD_URL, user, "secret", folder, filename, notes_content)

    assert len(responses.calls) == 3
    assert responses.calls[0].request.method == "MKCOL"
    assert responses.calls[0].request.url == f"{dav_base}/Talk"
    assert responses.calls[1].request.method == "MKCOL"
    assert responses.calls[1].request.url == f"{dav_base}/Talk/Notes"
    put_call = responses.calls[2]
    assert put_call.request.body == notes_content.encode()
