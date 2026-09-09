"""Tests for the retained-recording replay tool."""

from notetaker.reprocess import parse_retained_name


def test_parses_token_date_and_name_from_retained_filename():
    token, call_date, name = parse_retained_name(
        "/data/notes/retained-audio/92gwpi2v-2026-08-25-brantley-gunn-jo-ai-notetaker.webm"
    )
    assert token == "92gwpi2v"
    assert call_date == "2026-08-25"
    assert name == "Brantley Gunn Jo Ai Notetaker"


def test_unparseable_name_still_yields_a_usable_conversation_name():
    token, call_date, name = parse_retained_name("/tmp/some-recording.webm")
    assert token == ""
    assert call_date == ""
    assert name == "Some Recording"
