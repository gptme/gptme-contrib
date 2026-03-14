"""Tests for post_session context_tier plumbing."""

from pathlib import Path

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
