"""Regression tests for login verification in AudioRecorder.

These tests ensure the bot NEVER silently joins a call as guest
when login fails. The root cause (fixed 2026-03-26): the login flow
used fixed sleeps instead of element waits, and never verified that
login actually succeeded before navigating to the Talk room.
"""

from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from notetaker.recorder import AudioRecorder


def _evaluate_dispatcher():
    """Return an evaluate() mock that answers based on the JS it is given.

    A fixed side_effect list breaks whenever the poll loop changes shape
    (it already did when the 60s grace period was added), which makes these
    tests fail for reasons unrelated to what they are testing.
    """

    async def _evaluate(js, *args, **kwargs):
        src = js if isinstance(js, str) else str(js)
        if "proxyInstalled" in src:
            return {
                "proxyInstalled": True,
                "rtcType": "function",
                "rtcAvailable": True,
                "gumAvailable": True,
            }
        if "gumCount" in src:
            return {
                "hasRecorder": True,
                "hasCtx": True,
                "chunks": 1,
                "pcCount": 1,
                "trackCount": 1,
                "gumCount": 1,
            }
        if "ctxState" in src:
            return {
                "hasRecorder": True,
                "chunks": 5,
                "pcCount": 1,
                "trackCount": 1,
                "ctxState": "running",
            }
        if "__notetaker_recorder !== null" in src:
            return True
        if "__nt_timeline" in src or "trackKeys" in src:
            return {
                "timeline": [],
                "labels": {},
                "trackKeys": [],
                "rmsSummary": {},
                "sigLabels": {},
                "domLabels": {},
                "sigMap": {},
                "wsUrls": [],
            }
        if "__notetaker_chunks" in src:
            return None
        return None

    return AsyncMock(side_effect=_evaluate)



def _make_recorder():
    return AudioRecorder(
        nextcloud_url="https://nc.example.com",
        user="ai_notetaker",
        password="secret",
        audio_dir="/tmp/test-audio",
        auth_method="nextcloud",
    )


def _mock_playwright_stack():
    """Build a mock Playwright browser/context/page stack."""
    page = AsyncMock()
    page.url = "https://nc.example.com/login"

    # Locator for the login form user field
    user_locator = AsyncMock()
    user_locator.first = AsyncMock()
    user_locator.first.wait_for = AsyncMock()
    user_locator.first.fill = AsyncMock()

    # Locator for error elements
    error_locator = AsyncMock()
    error_locator.first = AsyncMock()
    error_locator.first.is_visible = AsyncMock(return_value=False)

    def locator_side_effect(selector):
        if "user" in selector:
            return user_locator
        if "error" in selector or "warning" in selector:
            return error_locator
        # Generic locator for overlays, join buttons, etc.
        generic = AsyncMock()
        generic.first = AsyncMock()
        generic.first.is_visible = AsyncMock(return_value=False)
        generic.first.click = AsyncMock()
        generic.count = AsyncMock(return_value=0)
        return generic

    page.locator = locator_side_effect
    page.fill = AsyncMock()
    page.click = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.evaluate = AsyncMock(
        return_value={
            "proxyInstalled": True,
            "rtcType": "function",
            "rtcAvailable": True,
            "gumAvailable": True,
        }
    )
    page.on = MagicMock()

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.add_init_script = AsyncMock()

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw = AsyncMock()
    pw.chromium = chromium

    pw_ctx = AsyncMock()
    pw_ctx.__aenter__ = AsyncMock(return_value=pw)
    pw_ctx.__aexit__ = AsyncMock(return_value=False)

    return pw_ctx, page, user_locator


@pytest.mark.asyncio
async def test_failed_login_raises_error():
    """If login doesn't redirect away from /login, record_call must raise."""
    recorder = _make_recorder()
    pw_ctx, page, user_locator = _mock_playwright_stack()

    # Simulate login failure: URL stays on /login forever
    type(page).url = PropertyMock(return_value="https://nc.example.com/login")

    with patch("playwright.async_api.async_playwright", return_value=pw_ctx):
        with pytest.raises(RuntimeError, match="login failed"):
            await recorder.record_call("room123", "Test Call")


@pytest.mark.asyncio
async def test_successful_login_proceeds():
    """Successful login (redirect to dashboard) should not raise."""
    recorder = _make_recorder()
    pw_ctx, page, user_locator = _mock_playwright_stack()

    # Simulate: first call returns /login (form submit), then /dashboard
    url_sequence = iter(
        [
            "https://nc.example.com/login",  # after wait_for_load_state
            "https://nc.example.com/apps/dashboard/",  # first verification check
        ]
    )
    type(page).url = PropertyMock(
        side_effect=lambda: next(url_sequence, "https://nc.example.com/apps/dashboard/")
    )

    # Make join button clickable so it doesn't hang
    join_locator = AsyncMock()
    join_locator.first = AsyncMock()
    join_locator.first.click = AsyncMock()
    join_locator.first.is_visible = AsyncMock(return_value=False)
    join_locator.count = AsyncMock(return_value=0)

    original_locator = page.locator

    def enhanced_locator(selector):
        if "Join call" in selector or "Start call" in selector:
            return join_locator
        return original_locator(selector)

    page.locator = enhanced_locator

    # Make presence report an empty call immediately (call ended)
    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch("notetaker.recorder._call_presence", return_value=(set(), 0, True), create=True),
    ):
        # Should complete without raising
        page.evaluate = _evaluate_dispatcher()
        audio_path, events, attendees = await recorder.record_call("room123", "Test Call")
        assert audio_path.endswith(".webm")
        assert events == []


@pytest.mark.asyncio
async def test_login_waits_for_form_elements():
    """Login must wait for Vue-rendered form elements, not just page load."""
    recorder = _make_recorder()
    pw_ctx, page, user_locator = _mock_playwright_stack()

    # Login succeeds
    type(page).url = PropertyMock(return_value="https://nc.example.com/apps/dashboard/")

    with (
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch("notetaker.recorder._call_presence", return_value=(set(), 0, True), create=True),
    ):
        page.evaluate = _evaluate_dispatcher()
        await recorder.record_call("room123", "Test Call")

    # Verify wait_for was called on the user field locator
    user_locator.first.wait_for.assert_called_once_with(state="visible", timeout=30000)


@pytest.mark.asyncio
async def test_login_error_message_detected():
    """If Nextcloud shows a login error, it must be raised with the message."""
    recorder = _make_recorder()
    pw_ctx, page, _ = _mock_playwright_stack()

    # URL stays on /login
    type(page).url = PropertyMock(return_value="https://nc.example.com/login")

    # Error element is visible with message
    error_locator = AsyncMock()
    error_locator.first = AsyncMock()
    error_locator.first.is_visible = AsyncMock(return_value=True)
    error_locator.first.text_content = AsyncMock(return_value="Wrong password")

    user_locator = AsyncMock()
    user_locator.first = AsyncMock()
    user_locator.first.wait_for = AsyncMock()
    user_locator.first.fill = AsyncMock()

    def locator_side_effect(selector):
        if "user" in selector:
            return user_locator
        if "error" in selector or "warning" in selector:
            return error_locator
        generic = AsyncMock()
        generic.first = AsyncMock()
        generic.first.is_visible = AsyncMock(return_value=False)
        return generic

    page.locator = locator_side_effect

    with patch("playwright.async_api.async_playwright", return_value=pw_ctx):
        with pytest.raises(RuntimeError, match="Wrong password"):
            await recorder.record_call("room123", "Test Call")
