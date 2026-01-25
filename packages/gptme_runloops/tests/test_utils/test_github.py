"""Tests for GitHub utilities (bot detection, loop prevention)."""

import json
import tempfile
from pathlib import Path

import pytest

from gptme_runloops.utils.github import (
    CommentLoopDetector,
    is_bot_review_author,
    is_bot_user,
)


class TestIsBotUser:
    """Tests for is_bot_user function."""

    def test_empty_username(self):
        """Empty username is not a bot."""
        assert is_bot_user("") is False
        assert is_bot_user(None) is False  # type: ignore

    def test_explicit_bot_type(self):
        """User with type 'Bot' is detected as bot."""
        assert is_bot_user("some-user", user_type="Bot") is True
        assert is_bot_user("normal-user", user_type="User") is False

    def test_bot_username_patterns(self):
        """Known bot username patterns are detected."""
        # Direct bot patterns
        assert is_bot_user("dependabot") is True
        assert is_bot_user("dependabot[bot]") is True
        assert is_bot_user("renovate") is True
        assert is_bot_user("renovate[bot]") is True
        assert is_bot_user("github-actions") is True
        assert is_bot_user("github-actions[bot]") is True
        assert is_bot_user("greptile[bot]") is True
        assert is_bot_user("coderabbit[bot]") is True
        assert is_bot_user("copilot") is True
        assert is_bot_user("codecov") is True
        assert is_bot_user("sonarcloud") is True
        assert is_bot_user("snyk-bot") is True

    def test_bot_suffix_patterns(self):
        """Bot suffix patterns are detected."""
        assert is_bot_user("my-custom-bot") is True
        assert is_bot_user("project_bot") is True
        assert is_bot_user("test-bot-user") is True

    def test_normal_users(self):
        """Normal usernames are not detected as bots."""
        assert is_bot_user("ErikBjare") is False
        assert is_bot_user("test-human-user") is False
        assert is_bot_user("octocat") is False
        assert is_bot_user("john_doe") is False
        assert is_bot_user("roberto") is False  # Contains 'bot' but not as pattern

    def test_case_insensitive(self):
        """Bot detection is case-insensitive."""
        assert is_bot_user("DEPENDABOT") is True
        assert is_bot_user("DependaBot") is True
        assert is_bot_user("MyBot") is True
        assert is_bot_user("MY-BOT") is True


class TestIsBotReviewAuthor:
    """Tests for is_bot_review_author function."""

    def test_empty_comment(self):
        """Empty or None comment is not from bot."""
        assert is_bot_review_author({}) is False
        assert is_bot_review_author(None) is False  # type: ignore

    def test_bot_user_in_comment(self):
        """Review from bot user is detected."""
        comment = {"user": {"login": "dependabot[bot]", "type": "Bot"}}
        assert is_bot_review_author(comment) is True

    def test_bot_author_field(self):
        """Review with author field (GraphQL style) is detected."""
        comment = {"author": {"login": "renovate[bot]"}}
        assert is_bot_review_author(comment) is True

    def test_normal_user_comment(self):
        """Review from normal user is not detected as bot."""
        comment = {"user": {"login": "ErikBjare", "type": "User"}}
        assert is_bot_review_author(comment) is False

    def test_missing_user_field(self):
        """Comment without user/author field is not from bot."""
        comment = {"body": "Some review comment"}
        assert is_bot_review_author(comment) is False


class TestCommentLoopDetector:
    """Tests for CommentLoopDetector class."""

    @pytest.fixture
    def temp_state_dir(self):
        """Create temporary directory for state files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_first_comment_allowed(self, temp_state_dir):
        """First comment should always be allowed."""
        detector = CommentLoopDetector(temp_state_dir)

        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="First comment",
            comment_type="update",
        )

        assert should_post is True
        assert reason == "OK"

    def test_second_identical_comment_allowed(self, temp_state_dir):
        """Second identical comment within window is allowed."""
        detector = CommentLoopDetector(temp_state_dir)

        # First comment
        detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Same content",
            comment_type="update",
        )

        # Second identical comment
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Same content",
            comment_type="update",
        )

        assert should_post is True
        assert reason == "OK"

    def test_third_identical_comment_blocked(self, temp_state_dir):
        """Third identical comment should be blocked (loop detected)."""
        detector = CommentLoopDetector(temp_state_dir)

        # First two comments
        for _ in range(2):
            detector.check_and_record(
                repo="owner/repo",
                pr_number=123,
                comment_content="Same content",
                comment_type="update",
            )

        # Third should be blocked
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Same content",
            comment_type="update",
        )

        assert should_post is False
        assert "Loop detected" in reason
        assert "2 identical/similar" in reason

    def test_different_content_allowed(self, temp_state_dir):
        """Different content should be allowed even after loop block."""
        detector = CommentLoopDetector(temp_state_dir)

        # Post same content twice
        for _ in range(2):
            detector.check_and_record(
                repo="owner/repo",
                pr_number=123,
                comment_content="Content A",
                comment_type="update",
            )

        # Different content should be allowed
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Completely different content",
            comment_type="different_type",
        )

        assert should_post is True
        assert reason == "OK"

    def test_same_type_counts(self, temp_state_dir):
        """Same comment type counts toward loop detection."""
        detector = CommentLoopDetector(temp_state_dir)

        # Post same type with different content
        for i in range(2):
            detector.check_and_record(
                repo="owner/repo",
                pr_number=123,
                comment_content=f"Content {i}",
                comment_type="ci_failure",
            )

        # Third of same type should be blocked
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Yet another content",
            comment_type="ci_failure",
        )

        assert should_post is False
        assert "Loop detected" in reason

    def test_different_prs_independent(self, temp_state_dir):
        """Different PRs should have independent state."""
        detector = CommentLoopDetector(temp_state_dir)

        # Post to PR 1
        for _ in range(2):
            detector.check_and_record(
                repo="owner/repo",
                pr_number=1,
                comment_content="Same content",
                comment_type="update",
            )

        # PR 2 should start fresh
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=2,
            comment_content="Same content",
            comment_type="update",
        )

        assert should_post is True
        assert reason == "OK"

    def test_clear_state(self, temp_state_dir):
        """clear_state should reset loop tracking for a PR."""
        detector = CommentLoopDetector(temp_state_dir)

        # Build up state
        for _ in range(2):
            detector.check_and_record(
                repo="owner/repo",
                pr_number=123,
                comment_content="Same content",
                comment_type="update",
            )

        # Clear state
        detector.clear_state(repo="owner/repo", pr_number=123)

        # Should be allowed again
        should_post, reason = detector.check_and_record(
            repo="owner/repo",
            pr_number=123,
            comment_content="Same content",
            comment_type="update",
        )

        assert should_post is True
        assert reason == "OK"

    def test_state_file_path(self, temp_state_dir):
        """State file path should be correctly formatted."""
        detector = CommentLoopDetector(temp_state_dir)

        # Record a comment
        detector.check_and_record(
            repo="example/repo",
            pr_number=42,
            comment_content="Test",
            comment_type="test",
        )

        # Check state file exists
        expected_file = temp_state_dir / "example-repo-pr-42-loop.json"
        assert expected_file.exists()

        # Verify content structure
        state = json.loads(expected_file.read_text())
        assert "comments" in state
        assert len(state["comments"]) == 1
        assert "hash" in state["comments"][0]
        assert "type" in state["comments"][0]
        assert "timestamp" in state["comments"][0]
