import responses
from notetaker.recorder import _slugify, _others_in_call, AudioRecorder


NEXTCLOUD_URL = "https://nc.example.com"
PARTICIPANTS_ENDPOINT = (
    f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/abc123/participants"
)


def test_slugify():
    assert _slugify("Daily Standup") == "daily-standup"
    assert _slugify("  Sprint Planning #3  ") == "sprint-planning-3"
    assert _slugify("One") == "one"
    assert _slugify("") == ""


def test_output_path_format():
    recorder = AudioRecorder.__new__(AudioRecorder)
    recorder.audio_dir = "/tmp/notetaker-audio"

    path = recorder._output_path("abc123", "Daily Standup")

    assert path.startswith("/tmp/notetaker-audio/")
    assert "daily-standup" in path
    assert path.endswith(".webm")


@responses.activate
def test_others_in_call_returns_true_when_others_present():
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    {"actorId": "bot", "inCall": 7},
                    {"actorId": "alice", "inCall": 7},
                ]
            }
        },
        status=200,
    )
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "secret"), "abc123", "bot") is True


@responses.activate
def test_others_in_call_returns_false_when_only_bot():
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={"ocs": {"data": [{"actorId": "bot", "inCall": 7}]}},
        status=200,
    )
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "secret"), "abc123", "bot") is False


@responses.activate
def test_others_in_call_returns_false_when_others_not_in_call():
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    {"actorId": "bot", "inCall": 7},
                    {"actorId": "alice", "inCall": 0},
                ]
            }
        },
        status=200,
    )
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "secret"), "abc123", "bot") is False


@responses.activate
def test_others_in_call_returns_true_on_api_failure():
    """On API failure, assume call is still active (safe default)."""
    responses.get(PARTICIPANTS_ENDPOINT, body=ConnectionError("timeout"))
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "secret"), "abc123", "bot") is True
