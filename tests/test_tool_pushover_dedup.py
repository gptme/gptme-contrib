"""Tests for pushover tool TTL dedup logic."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# scripts/ is in mypy_path but not always on sys.path when pytest runs from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import tool_pushover  # noqa: E402  (after sys.path patch)


@pytest.fixture(autouse=True)
def tmp_dedup_dir(tmp_path, monkeypatch):
    """Redirect dedup marker dir to a temp directory for test isolation."""
    monkeypatch.setattr(
        tool_pushover, "PUSHOVER_DEDUP_DIR", tmp_path / "pushover-dedup"
    )
    return tmp_path / "pushover-dedup"


def test_first_send_not_duplicate():
    assert not tool_pushover._is_recent_duplicate("Test", "Hello")


def test_second_send_within_ttl_is_duplicate():
    tool_pushover._mark_sent("Test", "Hello")
    assert tool_pushover._is_recent_duplicate("Test", "Hello")


def test_expired_marker_not_duplicate(tmp_dedup_dir):
    tool_pushover._mark_sent("Test", "Hello")
    marker = tmp_dedup_dir / f"{tool_pushover._dedup_key('Test', 'Hello')}.txt"
    old_time = time.time() - 31 * 60
    os.utime(marker, (old_time, old_time))
    assert not tool_pushover._is_recent_duplicate("Test", "Hello")


def test_different_message_not_duplicate():
    tool_pushover._mark_sent("Test", "Hello")
    assert not tool_pushover._is_recent_duplicate("Test", "Different message")


@patch("tool_pushover.requests.post")
def test_execute_deduplicates_repeat_call(mock_post, tmp_dedup_dir):
    mock_post.return_value = MagicMock(status_code=200)
    with (
        patch("tool_pushover.PUSHOVER_USER_KEY", "user"),
        patch("tool_pushover.PUSHOVER_API_TOKEN", "token"),
    ):
        result1 = tool_pushover.execute(
            None, None, {"title": "Alert", "message": "Down"}
        )
        assert "sent successfully" in result1.content

        result2 = tool_pushover.execute(
            None, None, {"title": "Alert", "message": "Down"}
        )
        assert "dedup" in result2.content.lower()
        assert mock_post.call_count == 1  # second call was blocked


@patch("tool_pushover.requests.post")
def test_execute_force_bypasses_dedup(mock_post, tmp_dedup_dir):
    mock_post.return_value = MagicMock(status_code=200)
    with (
        patch("tool_pushover.PUSHOVER_USER_KEY", "user"),
        patch("tool_pushover.PUSHOVER_API_TOKEN", "token"),
    ):
        tool_pushover.execute(None, None, {"title": "Alert", "message": "Down"})
        result = tool_pushover.execute(
            None, None, {"title": "Alert", "message": "Down", "force": "true"}
        )
        assert "sent successfully" in result.content
        assert mock_post.call_count == 2
