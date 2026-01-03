"""Tests for attention_history plugin."""

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_history_file(tmp_path):
    """Create temporary history file."""
    history_file = tmp_path / ".gptme" / "attention_history.jsonl"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with patch(
        "gptme_attention_history.tools.attention_history.HISTORY_FILE", history_file
    ):
        yield history_file


@pytest.fixture
def reset_session():
    """Reset session before each test."""
    from gptme_attention_history.tools import attention_history

    attention_history._current_session_id = None
    yield
    attention_history._current_session_id = None


def test_record_turn(temp_history_file, reset_session):
    """Test recording a turn."""
    from gptme_attention_history.tools.attention_history import record_turn

    result = record_turn(
        turn_number=1,
        hot_files=["file1.md", "file2.md"],
        warm_files=["file3.md"],
        activated_keywords=["test"],
        message_preview="test message",
    )

    assert "Recorded turn 1" in result
    assert "2 HOT" in result

    # Verify file content
    with open(temp_history_file) as f:
        entry = json.loads(f.readline())
    assert entry["turn_number"] == 1
    assert len(entry["hot_files"]) == 2


def test_query_session(temp_history_file, reset_session):
    """Test querying session history."""
    from gptme_attention_history.tools.attention_history import (
        record_turn,
        query_session,
        start_new_session,
    )

    start_new_session("test_session")
    record_turn(turn_number=1, hot_files=["file1.md"])
    record_turn(turn_number=2, hot_files=["file2.md"])

    entries = query_session("test_session")
    assert len(entries) == 2


def test_query_file(temp_history_file, reset_session):
    """Test querying file statistics."""
    from gptme_attention_history.tools.attention_history import record_turn, query_file

    record_turn(turn_number=1, hot_files=["tracked.md"])
    record_turn(turn_number=2, hot_files=["tracked.md"])
    record_turn(turn_number=3, hot_files=[], warm_files=["tracked.md"])

    stats = query_file("tracked.md")
    assert stats["hot_count"] == 2
    assert stats["warm_count"] == 1
    assert stats["total_appearances"] == 3


def test_query_coactivation(temp_history_file, reset_session):
    """Test co-activation analysis."""
    from gptme_attention_history.tools.attention_history import (
        record_turn,
        query_coactivation,
    )

    # Files A and B appear together
    record_turn(turn_number=1, hot_files=["a.md", "b.md"])
    record_turn(turn_number=2, hot_files=["a.md", "b.md"])
    record_turn(turn_number=3, hot_files=["a.md", "c.md"])

    pairs = query_coactivation()

    # A-B should appear most often
    assert len(pairs) > 0
    top_pair = pairs[0]
    assert "a.md" in [top_pair["file1"], top_pair["file2"]]
    assert "b.md" in [top_pair["file1"], top_pair["file2"]]
    assert top_pair["count"] == 2


def test_query_keyword_effectiveness(temp_history_file, reset_session):
    """Test keyword effectiveness analysis."""
    from gptme_attention_history.tools.attention_history import (
        record_turn,
        query_keyword_effectiveness,
    )

    record_turn(turn_number=1, hot_files=["file.md"], activated_keywords=["git"])
    record_turn(turn_number=2, hot_files=["file.md"], activated_keywords=["git"])
    record_turn(turn_number=3, hot_files=["other.md"], activated_keywords=["python"])

    keywords = query_keyword_effectiveness()

    assert len(keywords) == 2
    git_kw = next(k for k in keywords if k["keyword"] == "git")
    assert git_kw["activation_count"] == 2


def test_get_summary(temp_history_file, reset_session):
    """Test getting summary statistics."""
    from gptme_attention_history.tools.attention_history import (
        record_turn,
        get_summary,
        start_new_session,
    )

    start_new_session("session1")
    record_turn(turn_number=1, hot_files=["a.md", "b.md"])
    record_turn(turn_number=2, hot_files=["c.md"])

    start_new_session("session2")
    record_turn(turn_number=1, hot_files=["d.md"])

    summary = get_summary()

    assert summary["total_sessions"] == 2
    assert summary["total_turns"] == 3
    assert summary["avg_hot_files_per_turn"] > 0


def test_clear_history(temp_history_file, reset_session):
    """Test clearing history."""
    from gptme_attention_history.tools.attention_history import (
        record_turn,
        clear_history,
        get_summary,
    )

    record_turn(turn_number=1, hot_files=["file.md"])
    clear_history()

    summary = get_summary()
    assert summary["total_turns"] == 0


def test_start_new_session(temp_history_file, reset_session):
    """Test starting new session."""
    from gptme_attention_history.tools.attention_history import (
        start_new_session,
        record_turn,
        query_session,
    )

    start_new_session("custom_session")
    record_turn(turn_number=1, hot_files=["file.md"])

    entries = query_session("custom_session")
    assert len(entries) == 1


def test_tool_spec_exists():
    """Test that tool spec is properly defined."""
    from gptme_attention_history.tools.attention_history import tool

    assert tool.name == "attention_history"
    assert tool.functions is not None
    assert len(tool.functions) == 9  # All functions registered
