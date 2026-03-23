"""Tests for post_session context_tier plumbing and signal fallbacks."""

from pathlib import Path
from unittest.mock import patch

import gptme_sessions.post_session as _post_session_mod
import pytest

from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore


def test_post_session_context_tier(tmp_path: Path):
    """context_tier is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="massive",
        duration_seconds=120,
    )
    assert result.record.context_tier == "massive"

    # Verify it persists through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].context_tier == "massive"


def test_post_session_context_tier_none(tmp_path: Path):
    """context_tier defaults to None when not provided."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=60,
    )
    assert result.record.context_tier is None


def test_post_session_context_tier_standard(tmp_path: Path):
    """context_tier='standard' is stored correctly."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="standard",
        duration_seconds=90,
    )
    assert result.record.context_tier == "standard"


def test_post_session_ab_group_tier_version(tmp_path: Path):
    """ab_group and tier_version are stored in SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        context_tier="massive",
        ab_group="treatment",
        tier_version="v2",
        duration_seconds=120,
    )
    assert result.record.ab_group == "treatment"
    assert result.record.tier_version == "v2"

    # Verify they persist through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].ab_group == "treatment"
    assert records[0].tier_version == "v2"


def test_post_session_ab_group_tier_version_none(tmp_path: Path):
    """ab_group and tier_version default to None when not provided."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=60,
    )
    assert result.record.ab_group is None
    assert result.record.tier_version is None


def test_post_session_ab_group_invalid(tmp_path: Path):
    """post_session raises ValueError for invalid ab_group values."""
    store = SessionStore(sessions_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid ab_group"):
        post_session(
            store=store,
            harness="claude-code",
            model="opus",
            ab_group="invalid-group",
            duration_seconds=60,
        )


def test_post_session_duration_fallback_from_signals(tmp_path: Path):
    """duration_seconds falls back to session_duration_s from trajectory signals when 0."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")  # must exist for signal extraction to run

    fake_signals = {"session_duration_s": 300, "productive": True, "deliverables": []}
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )
    assert result.record.duration_seconds == 300


def test_post_session_model_fallback_from_signals(tmp_path: Path):
    """model falls back to usage.model from trajectory signals when 'unknown'."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {"model": "claude-sonnet-4-6", "total_tokens": 1000},
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )
    assert result.record.model == "claude-sonnet-4-6"
