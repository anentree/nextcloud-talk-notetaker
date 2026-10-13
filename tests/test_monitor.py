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


NOTIFICATIONS_ENDPOINT = (
    f"{NEXTCLOUD_URL}/ocs/v2.php/apps/notifications/api/v2/notifications"
)


def make_ring(nid, token, object_type="call"):
    return {
        "notification_id": nid,
        "app": "spreed",
        "object_type": object_type,
        "object_id": token,
    }


@responses.activate
def test_call_notification_rejoins_a_call_already_seen():
    room_data = {"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}}
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
    responses.get(NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": []}}, status=200)
    responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
    responses.get(
        NOTIFICATIONS_ENDPOINT,
        json={"ocs": {"data": [make_ring(7, "abc123")]}},
        status=200,
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/7", status=200)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert len(monitor.check_for_new_calls()) == 1
    # Same call, still active: only the ring makes it a new call again.
    again = monitor.check_for_new_calls()

    assert [r["token"] for r in again] == ["abc123"]


@responses.activate
def test_call_notification_is_acted_on_once():
    room_data = {"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}}
    ring = {"ocs": {"data": [make_ring(7, "abc123")]}}
    for _ in range(2):
        responses.get(ROOMS_ENDPOINT, json=room_data, status=200)
        responses.get(NOTIFICATIONS_ENDPOINT, json=ring, status=200)
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/7", status=500)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert len(monitor.check_for_new_calls()) == 1
    assert len(monitor.check_for_new_calls()) == 0


@responses.activate
def test_call_notification_ignored_when_no_call_is_active():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=False)]}},
        status=200,
    )
    responses.get(
        NOTIFICATIONS_ENDPOINT,
        json={"ocs": {"data": [make_ring(7, "abc123")]}},
        status=200,
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/7", status=200)

    assert CallMonitor(NEXTCLOUD_URL, "bot", "secret").check_for_new_calls() == []


@responses.activate
def test_chat_notifications_do_not_trigger_a_join():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=False)]}},
        status=200,
    )
    responses.get(
        NOTIFICATIONS_ENDPOINT,
        json={"ocs": {"data": [make_ring(7, "abc123", object_type="chat")]}},
        status=200,
    )

    assert CallMonitor(NEXTCLOUD_URL, "bot", "secret").check_for_new_calls() == []


@responses.activate
def test_notification_outage_does_not_break_detection():
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}},
        status=200,
    )
    responses.get(NOTIFICATIONS_ENDPOINT, status=503)

    active = CallMonitor(NEXTCLOUD_URL, "bot", "secret").check_for_new_calls()

    assert [r["token"] for r in active] == ["abc123"]


@responses.activate
def test_no_notifications_204_is_not_an_error(caplog):
    responses.get(
        ROOMS_ENDPOINT,
        json={"ocs": {"data": [make_room("abc123", "Standup", has_call=False)]}},
        status=200,
    )
    responses.get(NOTIFICATIONS_ENDPOINT, status=204)

    with caplog.at_level("WARNING"):
        assert CallMonitor(NEXTCLOUD_URL, "bot", "secret").check_for_new_calls() == []
    assert "Could not fetch call notifications" not in caplog.text


@responses.activate
def test_other_call_waiting_ignores_the_current_room():
    responses.get(
        ROOMS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    make_room("mine", "A", has_call=True),
                    make_room("idle", "B", has_call=False),
                ]
            }
        },
        status=200,
    )
    responses.get(
        ROOMS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    make_room("mine", "A", has_call=True),
                    make_room("next", "C", has_call=True),
                ]
            }
        },
        status=200,
    )
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.other_call_waiting("mine") is False
    assert monitor.other_call_waiting("mine") is True
