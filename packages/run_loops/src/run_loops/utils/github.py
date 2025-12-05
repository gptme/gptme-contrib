"""GitHub utilities for project monitoring.

Includes:
- Bot detection for review authors
- Comment loop prevention
- Review thread analysis
"""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional


# Known bot patterns in GitHub usernames
BOT_USERNAME_PATTERNS = [
    "bot",
    "-bot",
    "_bot",
    "[bot]",
    "github-actions",
    "dependabot",
    "renovate",
    "greptile",
    "coderabbit",
    "copilot",
    "codecov",
    "sonarcloud",
    "snyk",
]


def is_bot_user(username: str, user_type: Optional[str] = None) -> bool:
    """Check if a GitHub user is a bot.

    Args:
        username: GitHub username
        user_type: Optional user type from GitHub API ('Bot', 'User', etc.)

    Returns:
        True if the user appears to be a bot
    """
    if not username:
        return False

    # Check explicit type from API
    if user_type and user_type == "Bot":
        return True

    # Check username patterns
    username_lower = username.lower()
    for pattern in BOT_USERNAME_PATTERNS:
        if pattern in username_lower:
            return True

    return False


def get_user_type(username: str, repo: str) -> Optional[str]:
    """Get user type from GitHub API.

    Args:
        username: GitHub username
        repo: Repository name (owner/repo) - used for API context

    Returns:
        User type string or None if not found
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"users/{username}", "--jq", ".type"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def is_bot_review_author(review_comment: dict) -> bool:
    """Check if a review comment is from a bot.

    Args:
        review_comment: Review comment dict from GitHub API

    Returns:
        True if the review is from a bot
    """
    if not review_comment:
        return False

    user = review_comment.get("user", {}) or review_comment.get("author", {})
    if not user:
        return False

    username = user.get("login", "")
    user_type = user.get("type", "")

    return is_bot_user(username, user_type)


class CommentLoopDetector:
    """Detects and prevents comment loops.

    Tracks comment hashes and counts to detect when we're
    posting the same content repeatedly.
    """

    MAX_IDENTICAL_COMMENTS = 2  # Break loop after this many identical comments
    LOOP_WINDOW_HOURS = 24  # Window for counting identical comments

    def __init__(self, state_dir: Path):
        """Initialize loop detector.

        Args:
            state_dir: Directory for storing loop state
        """
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _get_state_file(self, repo: str, pr_number: int) -> Path:
        """Get state file path for a PR."""
        return self.state_dir / f"{repo.replace('/', '-')}-pr-{pr_number}-loop.json"

    def _hash_content(self, content: str) -> str:
        """Create hash of comment content."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def check_and_record(
        self, repo: str, pr_number: int, comment_content: str, comment_type: str
    ) -> tuple[bool, str]:
        """Check if posting would create a loop, and record if not.

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number
            comment_content: Content of the comment to post
            comment_type: Type of comment (e.g., "update", "ci_failure", "review_response")

        Returns:
            Tuple of (should_post: bool, reason: str)
        """
        state_file = self._get_state_file(repo, pr_number)
        content_hash = self._hash_content(comment_content)
        now = datetime.now()

        # Load existing state
        state: dict[str, list[dict[str, object]]] = {"comments": []}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                pass

        # Clean old entries outside window
        cutoff = now.timestamp() - (self.LOOP_WINDOW_HOURS * 3600)
        state["comments"] = [
            c
            for c in state.get("comments", [])
            if c.get("timestamp", 0) > cutoff  # type: ignore[operator]
        ]

        # Count identical comments in window
        identical_count = sum(
            1
            for c in state["comments"]
            if c.get("hash") == content_hash or c.get("type") == comment_type
        )

        if identical_count >= self.MAX_IDENTICAL_COMMENTS:
            return (
                False,
                f"Loop detected: {identical_count} identical/similar comments in {self.LOOP_WINDOW_HOURS}h window",
            )

        # Record this comment
        state["comments"].append(
            {
                "hash": content_hash,
                "type": comment_type,
                "timestamp": now.timestamp(),
            }
        )

        # Save state
        try:
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

        return (True, "OK")

    def clear_state(self, repo: str, pr_number: int) -> None:
        """Clear loop state for a PR (e.g., when PR is merged/closed)."""
        state_file = self._get_state_file(repo, pr_number)
        if state_file.exists():
            state_file.unlink()


def get_review_threads(repo: str, pr_number: int) -> list[dict]:
    """Get review threads from a PR.

    Args:
        repo: Repository name (owner/repo)
        pr_number: PR number

    Returns:
        List of review thread dicts
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "reviewThreads",
                "--jq",
                ".reviewThreads",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0 and result.stdout.strip():
            threads: list[dict] = json.loads(result.stdout)
            return threads
    except Exception:
        pass

    return []


def has_unresolved_bot_reviews(repo: str, pr_number: int) -> tuple[bool, list[str]]:
    """Check if PR has unresolved review threads from bots.

    Args:
        repo: Repository name (owner/repo)
        pr_number: PR number

    Returns:
        Tuple of (has_bot_reviews: bool, bot_usernames: list)
    """
    threads = get_review_threads(repo, pr_number)
    bot_usernames: list[str] = []

    for thread in threads:
        # Skip resolved threads
        if thread.get("isResolved", False):
            continue

        # Check first comment author (thread starter)
        comments = thread.get("comments", {}).get("nodes", [])
        if comments:
            first_comment = comments[0]
            author = first_comment.get("author", {})
            username = author.get("login", "")

            if is_bot_user(username):
                if username not in bot_usernames:
                    bot_usernames.append(username)

    return (len(bot_usernames) > 0, bot_usernames)
