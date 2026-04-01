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


@responses.activate
def test_clear_token_allows_redetection():
    """After clear_token, an ongoing call should be re-detected as new."""
    room_data = {"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}}
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    first = monitor.check_for_new_calls()
    assert len(first) == 1

    monitor.clear_token("abc123")
    second = monitor.check_for_new_calls()
    assert len(second) == 1
    assert second[0]["token"] == "abc123"


def test_clear_token_nonexistent_is_safe():
    """clear_token on a token not in the active set should not raise."""
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    monitor.clear_token("nonexistent")  # Should not raise


@responses.activate
def test_clear_token_only_affects_specified_room():
    """Clearing one token should not affect detection of other active calls."""
    room_data = {
        "ocs": {
            "data": [
                make_room("abc123", "Standup", has_call=True),
                make_room("def456", "Retro", has_call=True),
            ]
        }
    }
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    first = monitor.check_for_new_calls()
    assert len(first) == 2

    monitor.clear_token("abc123")
    second = monitor.check_for_new_calls()
    # Only abc123 should be re-detected, def456 is still tracked
    assert len(second) == 1
    assert second[0]["token"] == "abc123"


@responses.activate
def test_retry_on_api_failure():
    """_get_rooms retries on failure and succeeds on subsequent attempt."""
    responses.get(ROOMS_ENDPOINT, body=ConnectionError("timeout"))
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
def test_all_retries_exhausted_returns_empty():
    """_get_rooms returns empty list after all retries fail."""
    for _ in range(3):
        responses.get(ROOMS_ENDPOINT, body=ConnectionError("timeout"))

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    active = monitor.check_for_new_calls()

    assert len(active) == 0
