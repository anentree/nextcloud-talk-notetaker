"""Regressions found by the second adversarial review of the incident fixes.

Each of these was a defect introduced BY a fix, or a gap the fix left open.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

from notetaker import spool
from notetaker.config import Config
from notetaker.main import prune_retained_audio
from notetaker.preflight import check_smtp
from notetaker.recorder import resolve_stream_labels


def make_cfg(**kw) -> Config:
    base = dict(
        nextcloud_url="https://nc.example.com",
        nextcloud_user="bot",
        nextcloud_password="secret",
        nextcloud_web_password="secret",
        gemini_api_key="gemini-key",
        smtp_host="host.docker.internal",
        smtp_port=587,
        smtp_from="bot@example.com",
    )
    base.update(kw)
    return Config(**base)


def test_preflight_honours_the_configured_tls_policy():
    """Preflight hardcoded "auto" while sends used the configured value.
    With SMTP_TLS_VERIFY=false the send would work but preflight would fail,
    crash-looping the container — the exact failure the policy exists to avoid.
    """
    cfg = make_cfg(smtp_host="smtp.example.com", smtp_tls_verify="false")
    with (
        patch("notetaker.preflight.smtplib.SMTP") as smtp_cls,
        patch("notetaker.preflight._tls_context") as tls,
    ):
        smtp = smtp_cls.return_value
        smtp.__enter__.return_value = smtp
        smtp.has_extn.return_value = True
        check_smtp(cfg)

    for call in tls.call_args_list:
        assert call.args[1] == "false", "preflight must use cfg.smtp_tls_verify"


def test_drain_caps_work_per_cycle(tmp_path):
    """drain() runs on the single-threaded poll loop. An unbounded backlog
    against a blackholed SMTP host would block call detection for minutes."""
    spool_dir = str(tmp_path / "spool")
    for i in range(10):
        spool.enqueue(spool_dir, {"recipients": [{"email": f"u{i}@x.com"}], "notes": ""})

    calls = {"n": 0}

    def always_fail(_env):
        calls["n"] += 1
        raise OSError("down")

    spool.drain(spool_dir, always_fail)
    assert calls["n"] == spool.MAX_ATTEMPTS_PER_DRAIN
    assert len(spool.pending(spool_dir)) == 10, "nothing is dropped, only deferred"


def test_signaling_claim_beats_a_dom_guess_of_the_same_name():
    """Previously both were demoted, losing a name we actually knew."""
    labels, known, _ord, _sn, conflicts = resolve_stream_labels(
        ["1:janus0", "2:janus0"],
        {"1:janus0": "Piyush", "2:janus0": "Piyush"},
        [],
        authoritative_keys={"1:janus0"},
    )
    assert labels["1:janus0"] == "Piyush", "the signaling-confirmed track keeps its name"
    assert labels["2:janus0"].startswith("Speaker")
    assert conflicts == 1
    assert known == 1


def test_reconnect_keeps_one_name_across_two_signalled_tracks():
    labels, _k, _o, _sn, conflicts = resolve_stream_labels(
        ["1:janus0", "2:janus0"],
        {"1:janus0": "Piyush", "2:janus0": "Piyush"},
        [],
        authoritative_keys={"1:janus0", "2:janus0"},
    )
    assert labels == {"1:janus0": "Piyush", "2:janus0": "Piyush"}
    assert conflicts == 0


def test_no_ordinal_guess_without_a_name_pool():
    """With attendance unknown the recorder passes an empty pool, so an
    unlabeled track must stay anonymous rather than borrow a roster name."""
    labels, _k, ordinal, speaker_n, _c = resolve_stream_labels(
        ["1:janus0"], {"1:janus0": None}, []
    )
    assert ordinal == 0
    assert speaker_n == 1
    assert labels["1:janus0"] == "Speaker 1"


def test_prune_removes_stale_in_progress_audio(tmp_path):
    """If record_call raises after a partial flush, nobody knows the path and
    the fragment sits in tmpfs (RAM) until the container restarts."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    stale = audio_dir / "2026-08-13-accounting-talk.webm"
    stale.write_bytes(b"partial")
    os.utime(stale, (time.time() - 24 * 3600, time.time() - 24 * 3600))

    fresh = audio_dir / "today.webm"
    fresh.write_bytes(b"in progress")

    cfg = make_cfg(audio_dir=str(audio_dir), state_dir=str(tmp_path / "state"))
    prune_retained_audio(cfg)

    assert not stale.exists(), "a day-old recording belongs to a dead call"
    assert fresh.exists(), "an in-progress recording must survive"


def test_prune_never_follows_symlinks_out_of_the_directory(tmp_path):
    outside = tmp_path / "important.md"
    outside.write_text("do not delete")

    retained = tmp_path / "state" / "retained-audio"
    retained.mkdir(parents=True)
    link = retained / "link.webm"
    link.symlink_to(outside)
    os.utime(link, (0, 0), follow_symlinks=False)

    cfg = make_cfg(state_dir=str(tmp_path / "state"), audio_dir=str(tmp_path / "none"))
    prune_retained_audio(cfg)

    assert outside.exists(), "the symlink target must never be deleted"


def test_prune_is_a_noop_when_directories_do_not_exist(tmp_path):
    cfg = make_cfg(
        state_dir=str(tmp_path / "missing"), audio_dir=str(tmp_path / "also-missing")
    )
    assert prune_retained_audio(cfg) == 0


def test_spool_drain_reports_counts(tmp_path):
    spool_dir = str(tmp_path / "spool")
    spool.enqueue(spool_dir, {"recipients": [{"email": "a@x.com"}], "notes": ""})
    sent, remaining = spool.drain(spool_dir, MagicMock(return_value={}))
    assert (sent, remaining) == (1, 0)
