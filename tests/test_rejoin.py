"""The bot must notice when it has been dropped from a call and rejoin.

2026-09-29, room itujzrjw: Neo's connection dropped 80 seconds into the
call, the SFU tore down the call's media rooms, and the bot's session "left
call" with it. Neo and Piyush rejoined and talked for 77 minutes; the bot
stayed on the Talk page, out of the call, recording the silence of its one
dead subscription. The loop only ever asked whether OTHER people were in the
call, and skipped its own participant entry, so nothing noticed.
"""

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
    assert "Start call" in join.dialog.selectors[0]
    assert all("Start call" not in sel for sel in join.dialog.selectors[1:])


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
