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


def test_different_user_key_not_duplicate():
    """Dedup is scoped per recipient — different user keys must not cross-suppress."""
    tool_pushover._mark_sent("Alert", "Down", user_key="user_a")
    # Same title/message but different recipient — must NOT be suppressed
    assert not tool_pushover._is_recent_duplicate("Alert", "Down", user_key="user_b")
    # Same recipient — must BE suppressed
    assert tool_pushover._is_recent_duplicate("Alert", "Down", user_key="user_a")


def test_mark_sent_atomic_concurrent(tmp_dedup_dir):
    """Second _mark_sent on existing marker updates mtime instead of raising."""
    tool_pushover._mark_sent("X", "Y")
    # Simulate a concurrent caller: marker already exists
    tool_pushover._mark_sent("X", "Y")  # Must not raise FileExistsError
    assert tool_pushover._is_recent_duplicate("X", "Y")


def test_try_claim_send_blocks_concurrent(tmp_dedup_dir):
    """_try_claim_send: only the first caller gets True; concurrent caller gets False."""
    assert tool_pushover._try_claim_send("Alert", "Down") is True
    # Second call within TTL — marker already exists, claim denied
    assert tool_pushover._try_claim_send("Alert", "Down") is False


def test_try_claim_send_expired_allows_resend(tmp_dedup_dir):
    """_try_claim_send: expired marker is cleared and new claim succeeds."""
    tool_pushover._try_claim_send("Alert", "Down")
    marker = tmp_dedup_dir / f"{tool_pushover._dedup_key('Alert', 'Down')}.txt"
    old_time = time.time() - 31 * 60
    os.utime(marker, (old_time, old_time))
    assert tool_pushover._try_claim_send("Alert", "Down") is True


def test_release_claim_allows_retry(tmp_dedup_dir):
    """After _release_claim, a subsequent _try_claim_send must succeed."""
    tool_pushover._try_claim_send("Alert", "Down")
    tool_pushover._release_claim("Alert", "Down")
    assert tool_pushover._try_claim_send("Alert", "Down") is True


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


@patch("tool_pushover.requests.post")
def test_execute_releases_claim_on_failure(mock_post, tmp_dedup_dir):
    """On send failure the pre-send claim is released so the next send can retry."""
    mock_post.return_value = MagicMock(status_code=500)
    with (
        patch("tool_pushover.PUSHOVER_USER_KEY", "user"),
        patch("tool_pushover.PUSHOVER_API_TOKEN", "token"),
    ):
        result1 = tool_pushover.execute(
            None, None, {"title": "Alert", "message": "Down"}
        )
        assert "couldn't be sent" in result1.content

        # Marker must have been released — next call should proceed, not be deduped
        mock_post.return_value = MagicMock(status_code=200)
        result2 = tool_pushover.execute(
            None, None, {"title": "Alert", "message": "Down"}
        )
        assert "sent successfully" in result2.content
        assert mock_post.call_count == 2
