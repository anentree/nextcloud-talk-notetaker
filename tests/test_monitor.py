from unittest.mock import patch
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


def _rooms(*calls):
    return {
        "ocs": {"data": [make_room(t, t, has_call=a) for t, a in calls]}
    }


@responses.activate
def test_ring_for_another_room_interrupts_and_is_joined_afterwards():
    responses.get(ROOMS_ENDPOINT, json=_rooms(("mine", True), ("next", True)))
    responses.get(
        NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": [make_ring(9, "next")]}}
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/9", status=200)
    # After the recording ends the main loop polls again.
    responses.get(ROOMS_ENDPOINT, json=_rooms(("next", True)))
    responses.get(NOTIFICATIONS_ENDPOINT, status=204)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.ring_for_other_room("mine") is True
    assert [r["token"] for r in monitor.check_for_new_calls()] == ["next"]


@responses.activate
def test_ring_for_the_current_room_does_not_interrupt():
    responses.get(
        NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": [make_ring(9, "mine")]}}
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/9", status=200)

    assert CallMonitor(NEXTCLOUD_URL, "bot", "secret").ring_for_other_room("mine") is False


@responses.activate
def test_ring_for_an_ended_call_does_not_interrupt():
    responses.get(
        NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": [make_ring(9, "gone")]}}
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/9", status=200)
    responses.get(ROOMS_ENDPOINT, json=_rooms(("mine", True), ("gone", False)))

    assert CallMonitor(NEXTCLOUD_URL, "bot", "secret").ring_for_other_room("mine") is False


@responses.activate
def test_rung_room_is_joined_before_the_room_that_was_left():
    responses.get(ROOMS_ENDPOINT, json=_rooms(("old", True), ("next", True)))
    responses.get(
        NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": [make_ring(9, "next")]}}
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/9", status=200)

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    # "old" was just abandoned (cleared) and is listed first by Talk.
    order = [r["token"] for r in monitor.check_for_new_calls()]

    assert order == ["next", "old"]


@responses.activate
def test_ring_survives_a_failed_room_lookup():
    responses.get(
        NOTIFICATIONS_ENDPOINT, json={"ocs": {"data": [make_ring(9, "next")]}}
    )
    responses.delete(f"{NOTIFICATIONS_ENDPOINT}/9", status=200)
    responses.get(ROOMS_ENDPOINT, status=500)
    responses.get(ROOMS_ENDPOINT, status=500)
    responses.get(ROOMS_ENDPOINT, status=500)
    responses.get(ROOMS_ENDPOINT, json=_rooms(("mine", True), ("next", True)))

    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    with patch("notetaker.monitor.time.sleep"):
        assert monitor.ring_for_other_room("mine") is False
    responses.get(NOTIFICATIONS_ENDPOINT, status=204)
    assert monitor.ring_for_other_room("mine") is True


# A room that shows a call with nobody but the bot in it: the bot's own session
# lingers ~30s after it leaves. Joining it made the bot chase the call it had
# just left and, once the session expired, start a new one that rang everyone
# (2026-10-27).
def participants_endpoint(token):
    return f"{ROOMS_ENDPOINT}/{token}/participants"


def participants(*in_call):
    return {
        "ocs": {
            "data": [{"actorId": "bot", "actorType": "users", "inCall": 7}]
            + [
                {"actorId": name, "actorType": "users", "inCall": flag}
                for name, flag in in_call
            ]
        }
    }


def rooms_with_call():
    return {"ocs": {"data": [make_room("abc123", "Standup", has_call=True)]}}


@responses.activate
def test_a_call_with_only_the_bot_in_it_is_not_joined():
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 0)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.check_for_new_calls() == []


@responses.activate
def test_a_call_with_a_person_in_it_is_joined():
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 7)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert [r["token"] for r in monitor.check_for_new_calls()] == ["abc123"]


@responses.activate
def test_a_person_joining_the_lingering_call_is_still_picked_up():
    """An empty call must not be remembered as seen, or a person who joins it
    a few seconds later would never be recorded."""
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 0)))
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 7)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.check_for_new_calls() == []
    assert [r["token"] for r in monitor.check_for_new_calls()] == ["abc123"]


@responses.activate
def test_a_failed_participants_check_still_joins_the_call():
    """A missed meeting costs more than a pointless join."""
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), status=500)
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert [r["token"] for r in monitor.check_for_new_calls()] == ["abc123"]


@responses.activate
def test_a_person_still_in_the_call_the_bot_left_early_is_rejoined():
    """clear_token exists for a call the bot left for another one: people are
    still in it, so it is re-detected."""
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 7)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert len(monitor.check_for_new_calls()) == 1
    monitor.clear_token("abc123")
    assert len(monitor.check_for_new_calls()) == 1


@responses.activate
def test_a_rung_call_is_joined_even_if_it_looks_empty():
    """"Send call notification" is a person asking the bot to come."""
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 0)))
    responses.get(
        f"{NEXTCLOUD_URL}/ocs/v2.php/apps/notifications/api/v2/notifications",
        json={
            "ocs": {
                "data": [
                    {
                        "notification_id": 5,
                        "app": "spreed",
                        "object_type": "call",
                        "object_id": "abc123",
                    }
                ]
            }
        },
    )
    responses.delete(
        f"{NEXTCLOUD_URL}/ocs/v2.php/apps/notifications/api/v2/notifications/5"
    )
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert [r["token"] for r in monitor.check_for_new_calls()] == ["abc123"]


@responses.activate
def test_an_empty_lingering_call_is_logged_once(caplog):
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 0)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    with caplog.at_level("INFO", logger="notetaker.monitor"):
        for _ in range(3):
            monitor.check_for_new_calls()
    assert sum("nobody is in it" in r.message for r in caplog.records) == 1


@responses.activate
def test_a_lingering_call_is_not_a_call_waiting_for_the_bot():
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 0)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.other_call_waiting("elsewhere") is False


@responses.activate
def test_a_call_with_a_person_in_it_is_a_call_waiting_for_the_bot():
    responses.get(ROOMS_ENDPOINT, json=rooms_with_call())
    responses.get(participants_endpoint("abc123"), json=participants(("neo", 7)))
    monitor = CallMonitor(NEXTCLOUD_URL, "bot", "secret")
    assert monitor.other_call_waiting("elsewhere") is True
