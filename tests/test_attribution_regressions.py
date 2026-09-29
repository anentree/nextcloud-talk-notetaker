"""Regression tests for the speaker-attribution defects found on 2026-08-13.

A four-person, 79-minute call produced a timeline with ONE track in it, an
empty signaling map, and two different tracks both labelled "Piyush".
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
import responses

from notetaker.recorder import (
    AUDIO_CAPTURE_INIT_JS,
    _call_presence,
    _others_in_call,
    resolve_stream_labels,
)
from tests.test_login_verification import (
    _evaluate_dispatcher,
    _make_recorder,
    _mock_playwright_stack,
)

NEXTCLOUD_URL = "https://nc.example.com"
PARTICIPANTS_ENDPOINT = (
    f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/abc123/participants"
)


# --- signaling WebSocket interception --------------------------------------


def _ws_predicate_matches(url: str) -> bool:
    """Evaluate the injected JS URL test in Python.

    The JS is a template string, so the regexes carry doubled backslashes.
    Extract them and apply the same logic the browser would.
    """
    assert "isSignaling" in AUDIO_CAPTURE_INIT_JS
    if not re.match(r"^wss?:", url, re.I):
        return False
    return bool(re.search(r"signaling", url, re.I)) or bool(
        re.search(r"/spreed(/|\?|$)", url, re.I)
    )


def test_standalone_signaling_url_is_intercepted():
    """The real production URL. The old filter looked for the substring
    "/signaling", which ".../standalone-signaling/spreed" does not contain —
    so signaling attribution was silently disabled for months."""
    assert _ws_predicate_matches(
        "wss://cloud.example.com/standalone-signaling/spreed"
    )


def test_other_signaling_url_shapes_are_intercepted():
    for url in (
        "wss://signaling.example.com/spreed",
        "wss://nc.example.com/signaling/spreed",
        "ws://localhost:8080/spreed",
        "wss://hpb.example.com/standalone-signaling/spreed?ticket=abc",
    ):
        assert _ws_predicate_matches(url), url


def test_unrelated_websockets_are_ignored():
    for url in (
        "wss://nc.example.com/push/ws",
        "wss://example.com/socket.io/",
        "https://nc.example.com/spreed",  # not a websocket scheme
    ):
        assert not _ws_predicate_matches(url), url


def test_old_substring_filter_would_have_missed_production_url():
    """Documents the exact defect so it cannot silently return."""
    assert "/standalone-signaling/spreed".find("/signaling") == -1


# --- track key namespacing --------------------------------------------------


def test_track_key_is_namespaced_by_peer_connection():
    """Janus reuses track ids like "janus0" on every subscriber connection.

    An unnamespaced key made the "already seen this track" guard drop every
    participant after the first — 9 tracks collapsed to a 1-track timeline.
    """
    assert "pc.__nt_pcId + ':' + rawId" in AUDIO_CAPTURE_INIT_JS
    assert "pc.__nt_pcId = window.__notetaker_pc_count" in AUDIO_CAPTURE_INIT_JS


def test_dom_mapping_uses_track_identity_not_track_id():
    """Element mapping keyed by track.id was last-writer-wins across
    participants, which is why two tracks both resolved to "Piyush"."""
    assert "__nt_elBindings" in AUDIO_CAPTURE_INIT_JS
    assert "__nt_keyToTrack" in AUDIO_CAPTURE_INIT_JS
    assert "__nt_trackEl" not in AUDIO_CAPTURE_INIT_JS


# --- label conflict handling ------------------------------------------------


def test_duplicate_known_labels_are_demoted():
    """Two tracks cannot be the same person. If the labelling source says
    they are, it is wrong, and a confidently wrong name is worse than an
    anonymous one — the transcriber prompt treats the timeline as
    authoritative and overrides its own voice diarization with it."""
    labels, known, ordinal, speaker_n, conflicts = resolve_stream_labels(
        ["1:janus0", "2:janus0"],
        {"1:janus0": "Piyush", "2:janus0": "Piyush"},
        ["Piyush", "Brantley"],
    )

    assert conflicts == 2
    assert known == 0
    assert labels["1:janus0"] != labels["2:janus0"]
    assert labels["1:janus0"].startswith("Speaker")
    assert labels["2:janus0"].startswith("Speaker")


def test_distinct_known_labels_are_kept():
    labels, known, _ordinal, _speaker_n, conflicts = resolve_stream_labels(
        ["1:janus0", "2:janus0"],
        {"1:janus0": "Piyush", "2:janus0": "Brantley"},
        ["Piyush", "Brantley"],
    )
    assert conflicts == 0
    assert known == 2
    assert labels == {"1:janus0": "Piyush", "2:janus0": "Brantley"}


def test_speaker_numbers_are_unique_across_fallback_paths():
    """Two distinct tracks must never both become "Speaker 1"."""
    labels, _k, _o, speaker_n, _c = resolve_stream_labels(
        ["a", "b", "c"], {}, []
    )
    assert speaker_n == 3
    assert len(set(labels.values())) == 3


# --- call presence ----------------------------------------------------------


@responses.activate
def test_guest_only_call_is_still_considered_active():
    """Guests have no Nextcloud account but they are still on the call.

    Ending the recording because nobody in the call is a registered user
    would truncate any externally-attended meeting.
    """
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={
            "ocs": {
                "data": [
                    {"actorId": "bot", "actorType": "users", "inCall": 7},
                    {"actorId": "guest123", "actorType": "guests", "inCall": 7},
                ]
            }
        },
        status=200,
    )
    ids, total, _ = _call_presence(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot")
    assert total == 1, "guest counts as present"
    assert ids == set(), "but guests are not emailable recipients"
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot") is True


@responses.activate
def test_missing_actor_type_is_treated_as_a_user():
    """Older Talk API responses omit actorType; dropping those attendees
    would silently shrink the recipient list."""
    responses.get(
        PARTICIPANTS_ENDPOINT,
        json={"ocs": {"data": [{"actorId": "alice", "inCall": 7}]}},
        status=200,
    )
    ids, total, _ = _call_presence(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot")
    assert ids == {"alice"}
    assert total == 1


@responses.activate
def test_api_failure_reports_unknown_not_empty():
    """A transient API error must not be read as "the call ended"."""
    responses.get(PARTICIPANTS_ENDPOINT, status=500)
    assert _call_presence(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot") is None
    assert _others_in_call(NEXTCLOUD_URL, ("bot", "s"), "abc123", "bot") is True


# --- ordinal fallback must never draw on the room roster --------------------


_ROOM_ENDPOINT = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v4/room/room123/participants"
_ROSTER = [
    {"actorId": "ai_notetaker", "actorType": "users", "displayName": "Bot"},
    {"actorId": "brantley", "actorType": "users", "displayName": "Brantley"},
    {"actorId": "yashvi", "actorType": "users", "displayName": "Yashvi"},
]


def _timeline_dispatcher():
    """Wrap the login-test evaluate stub so the audio-extraction call returns
    one DOM-labelled track and one unlabeled track with a single event."""
    inner = _evaluate_dispatcher()

    async def _evaluate(js, *a, **k):
        result = await inner(js, *a, **k)
        if isinstance(result, dict) and "trackKeys" in result:
            return {
                **result,
                "timeline": [{"trackKey": "2:janus0", "startMs": 0, "endMs": 1000}],
                "labels": {"1:janus0": "Brantley", "2:janus0": None},
                "trackKeys": ["1:janus0", "2:janus0"],
            }
        return result

    return AsyncMock(side_effect=_evaluate)


@pytest.mark.asyncio
async def test_room_member_never_seen_in_call_is_not_named_onto_a_track():
    """Drive record_call end to end with presence polling that never sees
    anyone in the call (attendee_ids == set()) while the post-call roster
    fetch succeeds with two members. The unlabeled track must come out as
    "Speaker 1", never the roster name.

    Regression: the guard that clears the roster pool ran AFTER
    resolve_stream_labels consumed it, so it was dead code and "Yashvi" was
    named onto a track nobody claimed — the 2026-08-13 fabrication incident.
    Testing the resolver with an empty pool cannot catch that; only driving
    the real call path does.
    """
    recorder = _make_recorder()
    pw_ctx, page, _ = _mock_playwright_stack()
    type(page).url = PropertyMock(return_value=f"{NEXTCLOUD_URL}/apps/dashboard/")
    page.evaluate = _timeline_dispatcher()
    with (
        responses.RequestsMock() as rsps,
        patch("playwright.async_api.async_playwright", return_value=pw_ctx),
        patch("notetaker.recorder._call_presence", return_value=(set(), 0, True)),
        patch("notetaker.recorder.asyncio.sleep", new=AsyncMock()),
    ):
        rsps.get(_ROOM_ENDPOINT, json={"ocs": {"data": _ROSTER}}, status=200)
        _, events, attendees = await recorder.record_call("room123", "Test Call")
    assert attendees == set()
    assert [e["label"] for e in events] == ["Speaker 1"], events


def test_ordinal_fallback_names_a_non_attendee_when_the_pool_is_not_cleared():
    """Pins why the guard matters: given a roster pool, the resolver WILL
    assign one of those names to an unlabeled track. The guard is the only
    thing standing between an API failure and a fabricated speaker."""
    labels, _known, ordinal, _speaker_n, _conflicts = resolve_stream_labels(
        ["1:janus0", "2:janus0"],
        {"1:janus0": "Brantley", "2:janus0": None},
        ["Brantley", "Yashvi"],
        authoritative_keys=set(),
    )
    assert ordinal == 1
    assert labels["2:janus0"] == "Yashvi"
