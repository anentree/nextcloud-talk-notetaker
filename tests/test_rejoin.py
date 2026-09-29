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
async def test_the_open_dialog_check_uses_the_visible_only_locator():
    """The unfiltered dialog locator can report a hidden modal as the first
    match; only the visible=true one decides whether a dialog is open."""
    dropped = ({"neo"}, 1, False)
    recorder, pw_ctx, join = _join_tracking_page()
    visible_only = MagicMock()
    visible_only.first = AsyncMock()
    visible_only.first.is_visible = AsyncMock(return_value=False)
    visible_only.first.click = AsyncMock()
    join.dialog.first.is_visible = AsyncMock(return_value=True)  # unfiltered
    route = join.page.locator

    def locator(selector):
        if "visible=true" in selector:
            return visible_only
        return route(selector)

    join.page.locator = locator
    seq = iter([dropped, dropped, ({"neo"}, 1, True)])
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, (set(), 0, True)),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
    ):
        await recorder.record_call("abc123", "Talk")
    visible_only.first.click.assert_not_called()
    assert join.first.click.await_count == 2  # first join + rejoin via top bar


@pytest.mark.asyncio
async def test_the_recorder_measures_its_time_in_the_call():
    here = ({"neo"}, 1, True)
    clock = iter([1000.0] + [1000.0 + 10 * i for i in range(1, 40)])
    recorder, pw_ctx, _ = _join_tracking_page()
    seq = iter([here] * 3)
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, (set(), 0, True)),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
        patch("notetaker.recorder.time.monotonic", side_effect=lambda: next(clock)),
    ):
        await recorder.record_call("abc123", "Talk")
    # Joined at 1000; the grace period (60s) is not counted.
    assert recorder.in_call_s is not None
    assert recorder.in_call_s < 100


# --- the silence warning, in the call's own chat -------------------------------


class _Clock:
    """monotonic() that moves only when the recorder sleeps."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


async def _record_with_chat(
    presence, voice_ago, replied=None, silence_alert_s=300, post_error=None
):
    """Run a call; returns (post mock, replied mock). ``voice_ago`` is the
    page's voiceAgoS per poll; ``replied`` answers each reply check."""
    recorder, pw_ctx, join = _join_tracking_page()
    recorder.silence_alert_s = silence_alert_s
    recorder.silence_repeat_s = 60
    base = _evaluate_dispatcher()
    ago = iter(voice_ago)

    async def evaluate(js, *a, **k):
        if "voiceAgoS" in str(js):
            return {"hasRecorder": True, "voiceAgoS": next(ago, 0.0)}
        return await base(js, *a, **k)

    join.page.evaluate = evaluate
    seq = iter(presence)
    ids = iter(range(500, 600))
    post = MagicMock(side_effect=post_error or (lambda *a: next(ids)))
    answers = iter(replied or [])
    reply = MagicMock(side_effect=lambda *a: next(answers, False))
    clock = _Clock()
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch(
            "notetaker.recorder._call_presence",
            side_effect=lambda *a, **k: next(seq, (set(), 0, True)),
        ),
        patch("notetaker.recorder.asyncio.sleep", new=clock.sleep),
        patch("notetaker.recorder.time.monotonic", new=clock.monotonic),
        patch("notetaker.recorder._post_chat", new=post),
        patch("notetaker.recorder._someone_replied", new=reply),
    ):
        await recorder.record_call("abc123", "Accounting Talk")
    return post, reply


def _warnings(post):
    from notetaker.recorder import SILENCE_ACK_MESSAGE

    return [c.args[3] for c in post.call_args_list if c.args[3] != SILENCE_ACK_MESSAGE]


@pytest.mark.asyncio
async def test_a_long_silence_is_announced_in_the_chat_every_minute():
    here = ({"neo", "piyush"}, 2, True)
    post, _ = await _record_with_chat([here] * 13, [301 + 10 * i for i in range(13)])
    warnings = _warnings(post)
    # Polls are 10s apart: a warning on the first silent poll, then one per
    # minute — polls 1, 7 and 13.
    assert len(warnings) == 3
    assert "NOT recording" in warnings[0] and "Reply" in warnings[0]
    assert post.call_args_list[0].args[2] == "abc123"


@pytest.mark.asyncio
async def test_a_reply_in_the_chat_stops_the_warnings():
    from notetaker.recorder import SILENCE_ACK_MESSAGE

    here = ({"neo"}, 1, True)
    # Reply checks run on polls 2..: the third one sees a reply.
    post, reply = await _record_with_chat(
        [here] * 20, [400] * 20, replied=[False, False, True]
    )
    assert len(_warnings(post)) == 1
    assert post.call_args_list[-1].args[3] == SILENCE_ACK_MESSAGE
    # Replies are looked for after the FIRST warning of the episode.
    assert all(c.args[4] == 500 for c in reply.call_args_list)
    assert reply.call_count == 3  # no more checks once acknowledged


@pytest.mark.asyncio
async def test_hearing_someone_again_starts_a_new_episode():
    here = ({"neo"}, 1, True)
    voice = [400, 5, 400]
    post, reply = await _record_with_chat(
        [here] * 3, voice, replied=[True, False, False]
    )
    # Poll 1 warns (id 500). Poll 2 hears someone: reset, no reply check.
    # Poll 3 is a new episode: a fresh warning, not the old acknowledgement.
    assert len(_warnings(post)) == 2
    reply.assert_not_called()


@pytest.mark.asyncio
async def test_no_warning_when_nobody_else_is_in_the_call():
    alone = (set(), 0, True)
    post, _ = await _record_with_chat([alone] * 6, [900] * 6)
    post.assert_not_called()


@pytest.mark.asyncio
async def test_the_warning_can_be_turned_off():
    here = ({"neo"}, 1, True)
    post, _ = await _record_with_chat([here] * 4, [900] * 4, silence_alert_s=0)
    post.assert_not_called()


@pytest.mark.asyncio
async def test_a_chat_failure_does_not_stop_the_recording_and_is_retried():
    here = ({"neo"}, 1, True)
    post, _ = await _record_with_chat(
        [here] * 3, [400] * 3, post_error=[RuntimeError("503"), 501, 502]
    )
    assert post.call_count >= 2  # tried again on the next poll


@pytest.mark.asyncio
async def test_with_no_voice_ever_the_silence_is_counted_from_joining():
    here = ({"neo"}, 1, True)
    post, _ = await _record_with_chat([here] * 40, [None] * 40)
    warnings = _warnings(post)
    assert warnings
    assert "for 5 minutes" in warnings[0]


@responses.activate
def test_post_chat_sends_the_message_and_returns_its_id():
    from notetaker.recorder import _post_chat

    url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1/chat/abc123"
    responses.post(url, json={"ocs": {"data": {"id": 777}}}, status=201)
    assert _post_chat(NEXTCLOUD_URL, ("bot", "s"), "abc123", "hello") == 777
    assert responses.calls[0].request.body == "message=hello"


@pytest.mark.parametrize(
    "messages,expected",
    [
        ([], False),
        ([{"actorId": "bot", "messageType": "comment", "systemMessage": ""}], False),
        (
            [{"actorId": "neo", "messageType": "system", "systemMessage": "call_left"}],
            False,
        ),
        ([{"actorId": "neo", "messageType": "comment", "systemMessage": ""}], True),
    ],
)
@responses.activate
def test_someone_replied_counts_only_a_person_writing(messages, expected):
    from notetaker.recorder import _someone_replied

    url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1/chat/abc123"
    responses.get(url, json={"ocs": {"data": messages}})
    assert (
        _someone_replied(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot", 500) is expected
    )
    query = responses.calls[0].request.url
    for part in (
        "lookIntoFuture=1",
        "lastKnownMessageId=500",
        "setReadMarker=0",
        "timeout=0",
    ):
        assert part in query


@responses.activate
def test_someone_replied_is_false_when_nothing_is_new():
    from notetaker.recorder import _someone_replied

    url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1/chat/abc123"
    responses.get(url, status=304)
    assert _someone_replied(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot", 500) is False
