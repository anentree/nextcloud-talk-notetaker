"""The bot must notice when it has been dropped from a call and rejoin.

2026-09-29, room itujzrjw: Neo's connection dropped 80 seconds into the
call, the SFU tore down the call's media rooms, and the bot's session "left
call" with it. Neo and Piyush rejoined and talked for 77 minutes; the bot
stayed on the Talk page, out of the call, recording the silence of its one
dead subscription. The loop only ever asked whether OTHER people were in the
call, and skipped its own participant entry, so nothing noticed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import responses

from notetaker.recorder import _call_presence
from tests.test_login_verification import (
    _evaluate_dispatcher,
    _make_recorder,
    _mock_playwright_stack,
)

NEXTCLOUD_URL = "https://nc.example.com"
PARTICIPANTS = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/abc123/participants"


@responses.activate
def test_presence_reports_whether_the_bot_itself_is_in_the_call():
    responses.get(
        PARTICIPANTS,
        json={
            "ocs": {
                "data": [
                    {"actorId": "bot", "actorType": "users", "inCall": 0},
                    {"actorId": "neo", "actorType": "users", "inCall": 7},
                ]
            }
        },
    )
    ids, total, self_in_call = _call_presence(
        NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot"
    )
    assert (ids, total, self_in_call) == ({"neo"}, 1, False)


@responses.activate
def test_presence_counts_the_bot_in_call_when_its_flag_is_set():
    responses.get(
        PARTICIPANTS,
        json={"ocs": {"data": [{"actorId": "bot", "actorType": "users", "inCall": 3}]}},
    )
    assert _call_presence(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot")[2] is True


def _join_tracking_page():
    """A Playwright stack whose top-bar join button and media-settings dialog
    count their clicks separately. By default no dialog ever appears."""
    recorder = _make_recorder()
    pw_ctx, page, _ = _mock_playwright_stack()
    type(page).url = PropertyMock(return_value="https://nc.example.com/apps/dashboard/")
    join = MagicMock()
    join.first = AsyncMock()
    join.first.click = AsyncMock()
    join.first.is_visible = AsyncMock(return_value=False)
    join.count = AsyncMock(return_value=0)
    join.selectors = []
    dialog = MagicMock()
    dialog.first = AsyncMock()
    dialog.first.click = AsyncMock()
    dialog.first.is_visible = AsyncMock(return_value=False)
    dialog.first.wait_for = AsyncMock(side_effect=TimeoutError("no dialog"))
    dialog.selectors = []
    join.dialog = dialog
    original = page.locator

    def locator(selector):
        if ".media-settings" in selector:
            dialog.selectors.append(selector)
            return dialog
        if "Join call" in selector or "Start call" in selector:
            join.selectors.append(selector)
            return join
        return original(selector)

    page.locator = locator
    page.evaluate = _evaluate_dispatcher()
    join.page = page
    return recorder, pw_ctx, join


async def _record(presence_sequence, setup=None):
    recorder, pw_ctx, join = _join_tracking_page()
    if setup:
        setup(join)
    ended = (set(), 0, True)
    seq = iter(presence_sequence)
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, ended),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
    ):
        await recorder.record_call("abc123", "Accounting Talk")
    return join


@pytest.mark.asyncio
async def test_a_bot_dropped_from_a_running_call_rejoins_it():
    here = ({"neo"}, 1, True)
    dropped = ({"neo", "piyush"}, 2, False)
    join = await _record([here, dropped, dropped, here, here])
    # One click for the first join, one more for the rejoin.
    assert join.first.click.await_count == 2


@pytest.mark.asyncio
async def test_one_missed_poll_is_not_enough_to_rejoin():
    """The flag can lag a join by a poll; a single miss is not a drop."""
    here = ({"neo"}, 1, True)
    dropped = ({"neo"}, 1, False)
    join = await _record([here, dropped, here, here])
    assert join.first.click.await_count == 1


@pytest.mark.asyncio
async def test_the_bot_keeps_trying_while_it_stays_out():
    dropped = ({"neo"}, 1, False)
    join = await _record([dropped] * 6)
    # Initial join, then a rejoin after every second consecutive miss.
    assert join.first.click.await_count == 4


@pytest.mark.asyncio
async def test_an_unknown_presence_never_triggers_a_rejoin():
    join = await _record([None] * 6)
    assert join.first.click.await_count == 1


@pytest.mark.asyncio
async def test_a_failing_rejoin_does_not_end_the_recording():
    recorder, pw_ctx, join = _join_tracking_page()
    join.first.click.side_effect = [None, TimeoutError("no button"), None]
    dropped = ({"neo"}, 1, False)
    here = ({"neo"}, 1, True)
    seq = iter([dropped, dropped, dropped, dropped, here])
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, (set(), 0, True)),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
    ):
        audio_path, _, attendees = await recorder.record_call("abc123", "Talk")
    assert audio_path.endswith(".webm")
    assert attendees == {"neo"}
    assert join.first.click.await_count == 3


@pytest.mark.asyncio
async def test_a_rejoin_can_never_click_leave_call():
    """The first join has a catch-all for any call button in the top bar.
    That also matches "Leave call", so a rejoin must not use it."""
    here = ({"neo"}, 1, True)
    dropped = ({"neo"}, 1, False)
    join = await _record([here, dropped, dropped, here])
    top_bar = [sel for sel in join.selectors if "top-bar" in sel]
    assert len(top_bar) == 1  # the first join only


@pytest.mark.asyncio
async def test_being_seen_back_in_the_call_resets_the_count():
    here = ({"neo"}, 1, True)
    dropped = ({"neo"}, 1, False)
    join = await _record([dropped, here, dropped, here])
    assert join.first.click.await_count == 1


@pytest.mark.asyncio
async def test_an_unknown_poll_neither_counts_nor_resets():
    dropped = ({"neo"}, 1, False)
    join = await _record([dropped, None, dropped, ({"neo"}, 1, True)])
    assert join.first.click.await_count == 2


@pytest.mark.asyncio
async def test_a_bot_alone_after_the_call_ends_never_rejoins():
    """With nobody else in the call, clicking would start a new call and
    ring the whole room."""
    alone = (set(), 0, False)
    join = await _record([alone] * 8)
    assert join.first.click.await_count == 1


@pytest.mark.asyncio
async def test_a_rejoin_never_clicks_start_call():
    here = ({"neo"}, 1, True)
    dropped = ({"neo"}, 1, False)
    join = await _record([here, dropped, dropped, here])
    # Each join builds a top-bar locator and a fallback locator.
    first, rejoin = join.selectors[:2], join.selectors[2:]
    assert all("Start call" in sel for sel in first)  # the check can see it
    assert len(rejoin) == 2
    assert all("Start call" not in sel for sel in rejoin)
    # Each join builds an open-dialog locator and a dialog locator.
    assert all("Start call" in sel for sel in join.dialog.selectors[:2])
    assert len(join.dialog.selectors) == 4
    assert all("Start call" not in sel for sel in join.dialog.selectors[2:])


@pytest.mark.asyncio
async def test_a_rejoin_clicks_through_the_media_settings_dialog():
    here = ({"neo"}, 1, True)
    dropped = ({"neo"}, 1, False)

    def dialog_appears(join):
        join.dialog.first.wait_for = AsyncMock()

    join = await _record([here, dropped, dropped, here], setup=dialog_appears)
    # Once for the first join, once for the rejoin.
    assert join.dialog.first.click.await_count == 2


@pytest.mark.asyncio
async def test_a_dialog_left_open_is_clicked_instead_of_the_covered_button():
    """A dialog that rendered late and was missed stays open over the top
    bar; the next rejoin must click the dialog, not the covered button."""
    dropped = ({"neo"}, 1, False)

    def dialog_left_open(join):
        join.dialog.first.is_visible = AsyncMock(return_value=True)

    join = await _record([dropped, dropped, ({"neo"}, 1, True)], setup=dialog_left_open)
    assert join.first.click.await_count == 1  # the first join only
    assert join.dialog.first.click.await_count == 1


# --- telling a human while the call can still be saved ------------------------


async def _record_with_voice(presence, voice_ago, silence_alert_s=300, alert_fn=None):
    recorder, pw_ctx, join = _join_tracking_page()
    recorder.silence_alert_s = silence_alert_s
    recorder.alert_fn = alert_fn or MagicMock()
    base = _evaluate_dispatcher()
    ago = iter(voice_ago)

    async def evaluate(js, *a, **k):
        if "voiceAgoS" in str(js):
            return {"hasRecorder": True, "voiceAgoS": next(ago, 0.0)}
        return await base(js, *a, **k)

    join.page.evaluate = evaluate
    seq = iter(presence)
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, (set(), 0, True)),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
    ):
        await recorder.record_call("abc123", "Accounting Talk")
    return recorder.alert_fn


@pytest.mark.asyncio
async def test_a_long_silence_with_people_in_the_call_alerts_once():
    here = ({"neo", "piyush"}, 2, True)
    alert = await _record_with_voice([here] * 6, [0, 100, 200, 301, 400, 500])
    alert.assert_called_once()
    silent_s, others, rejoins = alert.call_args.args
    assert silent_s == 301 and others == 2 and rejoins == 0


@pytest.mark.asyncio
async def test_a_new_silence_after_voice_alerts_again():
    here = ({"neo"}, 1, True)
    alert = await _record_with_voice([here] * 6, [301, 400, 5, 10, 320, 330])
    assert alert.call_count == 2


@pytest.mark.asyncio
async def test_no_alert_when_nobody_else_is_in_the_call():
    alone = (set(), 0, True)
    alert = await _record_with_voice([alone] * 6, [900] * 6)
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_the_silence_alert_can_be_turned_off():
    here = ({"neo"}, 1, True)
    alert = await _record_with_voice([here] * 4, [900] * 4, silence_alert_s=0)
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_alert_does_not_stop_the_recording():
    here = ({"neo"}, 1, True)
    alert = MagicMock(side_effect=RuntimeError("smtp down"))
    await _record_with_voice([here] * 3, [900] * 3, alert_fn=alert)
    alert.assert_called_once()


def test_the_open_dialog_filter_applies_to_the_whole_list():
    """Playwright splits on ">>" before ",": a filter per alternative parses
    into a chain that can never match (the 2a83186 regression)."""
    import asyncio as aio

    recorder = _make_recorder()
    page = MagicMock()
    seen = []

    def locator(selector):
        seen.append(selector)
        loc = MagicMock()
        loc.first = AsyncMock()
        loc.first.is_visible = AsyncMock(return_value=True)
        return loc

    page.locator = locator
    aio.run(recorder._join_call(page, rejoin=True))
    [open_dialog] = [sel for sel in seen if "visible=true" in sel]
    assert open_dialog.count(">>") == 1
    assert open_dialog.endswith(" >> visible=true")


@pytest.mark.asyncio
async def test_with_no_voice_ever_the_silence_is_counted_from_joining():
    here = ({"neo"}, 1, True)
    clock = iter([1000.0] + [1000.0 + 60 * i for i in range(1, 20)])
    with patch("notetaker.recorder.time.monotonic", side_effect=lambda: next(clock)):
        alert = await _record_with_voice([here] * 8, [None] * 8)
    alert.assert_called_once()
    assert alert.call_args.args[0] >= 300


@pytest.mark.asyncio
async def test_a_slow_alert_does_not_hold_up_the_polls():
    import threading

    here = ({"neo"}, 1, True)
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    polls = []
    polled_during_alert = []

    def slow_alert(*args):
        started.set()
        release.wait(5)
        finished.set()

    recorder, pw_ctx, join = _join_tracking_page()
    recorder.silence_alert_s = 300
    recorder.alert_fn = slow_alert
    base = _evaluate_dispatcher()

    async def evaluate(js, *a, **k):
        if "voiceAgoS" in str(js):
            return {"hasRecorder": True, "voiceAgoS": 900.0}
        return await base(js, *a, **k)

    join.page.evaluate = evaluate
    seq = iter([here] * 5)

    def presence(*a, **k):
        polls.append(1)
        if started.is_set() and not finished.is_set():
            polled_during_alert.append(len(polls))
        if len(polls) == 5:
            release.set()
        return next(seq, (set(), 0, True))

    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch("notetaker.recorder._call_presence", side_effect=presence),
    ):
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0)

        with patch("notetaker.recorder.asyncio.sleep", new=fast_sleep):
            await recorder.record_call("abc123", "Talk")
    # Polls kept running while the alert was still being sent.
    assert polled_during_alert
    assert finished.is_set()
