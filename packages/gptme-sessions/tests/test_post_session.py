"""Tests for post_session context_tier plumbing and signal fallbacks."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore

# gptme_sessions/__init__.py re-exports 'post_session' (function), shadowing the
# submodule attribute on the package.  `import gptme_sessions.post_session as mod`
# resolves via getattr(gptme_sessions, 'post_session') → the function, not the module.
# from gptme_sessions.post_session import post_session ensures the module is in
# sys.modules, so we can retrieve it directly for patch.object calls.
_post_session_mod = sys.modules["gptme_sessions.post_session"]


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


def test_post_session_exit_code_persisted(tmp_path: Path):
    """exit_code is stored in the SessionRecord when passed to post_session."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        exit_code=124,
        duration_seconds=3000,
    )
    assert result.record.exit_code == 124

    # Verify it persists through store reload
    store2 = SessionStore(sessions_dir=tmp_path)
    records = store2.load_all()
    assert len(records) == 1
    assert records[0].exit_code == 124


def test_post_session_exit_code_defaults_zero(tmp_path: Path):
    """exit_code defaults to 0 when not specified."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="opus",
        duration_seconds=60,
    )
    assert result.record.exit_code == 0


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


def test_post_session_populates_productivity_grade(tmp_path: Path):
    """post_session mirrors the scalar trajectory grade into grades.productivity."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": ["feat: ship thing (abc1234)"],
        "grade": 0.68,
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.record.trajectory_grade == 0.68
    assert result.record.grades == {"productivity": 0.68}

    records = store.load_all()
    assert records[0].grades == {"productivity": 0.68}


def test_post_session_persists_usage_fields(tmp_path: Path):
    """Trajectory usage totals should be written into the canonical session record."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_creation_tokens": 30,
            "cache_read_tokens": 600,
            "total_tokens": 795,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.token_count == 795
    assert result.record.input_tokens == 120
    assert result.record.output_tokens == 45
    assert result.record.cache_creation_tokens == 30
    assert result.record.cache_read_tokens == 600

    records = store.load_all()
    assert records[0].input_tokens == 120
    assert records[0].output_tokens == 45
    assert records[0].cache_creation_tokens == 30
    assert records[0].cache_read_tokens == 600


def test_post_session_partial_usage_fields_stored_as_none(tmp_path: Path):
    """Absent usage sub-fields must be None, not 0, to avoid corrupting analytics."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    # Older trajectory format: only total_tokens present, no breakdown keys
    fake_signals = {
        "session_duration_s": 60,
        "productive": True,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "total_tokens": 500,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            duration_seconds=0,
            trajectory_path=fake_traj,
        )

    assert result.token_count == 500
    # Absent breakdown keys must be None, not 0
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cache_creation_tokens is None
    assert result.cache_read_tokens is None

    records = store.load_all()
    assert records[0].input_tokens is None
    assert records[0].output_tokens is None
    assert records[0].cache_creation_tokens is None
    assert records[0].cache_read_tokens is None


def test_post_session_preserves_zero_token_usage(tmp_path: Path):
    """Zero-token trajectories are stored as token_count=0, not dropped as missing."""
    store = SessionStore(sessions_dir=tmp_path)
    fake_traj = tmp_path / "trajectory.jsonl"
    fake_traj.write_text("")

    fake_signals = {
        "session_duration_s": 60,
        "productive": False,
        "deliverables": [],
        "usage": {
            "model": "claude-sonnet-4-6",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        },
    }
    with patch.object(_post_session_mod, "extract_from_path", return_value=fake_signals):
        result = post_session(
            store=store,
            harness="claude-code",
            model="sonnet",
            duration_seconds=0,
            trajectory_path=fake_traj,
            exit_code=1,
        )

    assert result.token_count == 0
    assert result.record.token_count == 0

    records = store.load_all()
    assert records[0].token_count == 0
