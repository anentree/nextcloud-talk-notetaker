import responses
from notetaker.monitor import CallMonitor


NEXTCLOUD_URL = "https://nc.example.com"
ROOMS_ENDPOINT = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room"


def make_room(token, name, has_call=False):
    return {
        "token": token,
        "displayName": name,
        "hasCall": has_call,
        "callFlag": 7 if has_call else 0,
    }


@responses.activate
def test_detect_new_active_call():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}},
        status=200,
    )

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    active = monitor.check_for_new_calls()

    assert len(active) == 1
    assert active[0]["token"] == "abc123"


@responses.activate
def test_same_call_not_reported_twice():
    room_data = {"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}}
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    first = monitor.check_for_new_calls()
    second = monitor.check_for_new_calls()

    assert len(first) == 1
    assert len(second) == 0


@responses.activate
def test_call_ended_allows_future_detection():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}},
        status=200,
    )
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=False)]}},
        status=200,
    )
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}},
        status=200,
    )

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert len(monitor.check_for_new_calls()) == 1
    assert len(monitor.check_for_new_calls()) == 0
    assert len(monitor.check_for_new_calls()) == 1


@responses.activate
def test_no_active_calls():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=False)]}},
        status=200,
    )

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    active = monitor.check_for_new_calls()

    assert len(active) == 0
