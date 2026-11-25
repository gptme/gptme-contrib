"""Structured waiting_for support for gptodo.

This module implements auto-resolution of external waiting conditions:
- pr_ci: Wait for PR CI checks to pass
- pr_merged: Wait for PR to be merged
- comment: Wait for comment matching a pattern
- time: Wait until a specific time

Waiting conditions can be specified in task metadata:

    waiting_for:
      type: pr_ci
      ref: "gptme/gptme#1217"

Or as a list of conditions:

    waiting_for:
      - type: pr_ci
        ref: "gptme/gptme#1217"
      - type: comment
        ref: "gptme/gptme#1217"
        pattern: "LGTM"
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from gptodo.frontmatter_compat import frontmatter


class WaitType(str, Enum):
    """Types of waiting conditions."""

    PR_CI = "pr_ci"  # Wait for PR CI to pass
    PR_MERGED = "pr_merged"  # Wait for PR to be merged
    COMMENT = "comment"  # Wait for comment matching pattern
    TIME = "time"  # Wait until specific time
    TASK = "task"  # Wait for another task (legacy string format)
    # A ``type:`` this version does not know (a typo, or a newer writer). It
    # never resolves and is written back exactly as read.
    UNKNOWN = "unknown"


@dataclass
class WaitCondition:
    """A structured waiting condition."""

    type: WaitType
    ref: str  # Reference (PR URL, task ID, ISO timestamp)
    pattern: str | None = None  # For comment type
    resolved: bool = False
    resolution_time: datetime | None = None
    error: str | None = None
    raw: dict | None = None  # Original mapping for an UNKNOWN type

    @classmethod
    def from_dict(cls, data: dict) -> "WaitCondition":
        """Create WaitCondition from dict (parsed YAML)."""
        try:
            wait_type = WaitType(data.get("type", "task"))
        except ValueError:
            wait_type = WaitType.UNKNOWN
        return cls(
            type=wait_type,
            ref=data.get("ref", ""),
            pattern=data.get("pattern"),
            raw=dict(data) if wait_type is WaitType.UNKNOWN else None,
        )

    @classmethod
    def from_string(cls, value: str) -> "WaitCondition":
        """Create WaitCondition from legacy string format."""
        # Check if it looks like a PR/issue URL
        if "github.com" in value or "#" in value:
            # Assume it's waiting for general resolution
            return cls(type=WaitType.TASK, ref=value)
        return cls(type=WaitType.TASK, ref=value)

    def to_dict(self) -> dict:
        """Convert to dict for YAML serialization."""
        if self.raw is not None:
            return dict(self.raw)
        result = {"type": self.type.value, "ref": self.ref}
        if self.pattern:
            result["pattern"] = self.pattern
        return result


def parse_waiting_for(metadata: dict) -> list[WaitCondition]:
    """Parse waiting_for field into list of WaitConditions.

    Handles both legacy string format and new structured format.
    """
    waiting_for = metadata.get("waiting_for")
    if not waiting_for:
        return []

    # Legacy string format
    if isinstance(waiting_for, str):
        return [WaitCondition.from_string(waiting_for)]

    # Single structured condition
    if isinstance(waiting_for, dict):
        return [WaitCondition.from_dict(waiting_for)]

    # List of conditions
    if isinstance(waiting_for, list):
        conditions = []
        for item in waiting_for:
            if isinstance(item, str):
                conditions.append(WaitCondition.from_string(item))
            elif isinstance(item, dict):
                conditions.append(WaitCondition.from_dict(item))
        return conditions

    return []


def parse_github_ref(ref: str) -> tuple[str, str, int]:
    """Parse GitHub reference into (owner, repo, number).

    Handles:
    - Full URL: https://github.com/gptme/gptme/pull/1217
    - Short form: gptme/gptme#1217
    - Issue URL: https://github.com/owner/repo/issues/123
    """
    # Full URL format
    if "github.com" in ref:
        # Extract from URL like https://github.com/owner/repo/pull/123
        parts = ref.replace("https://github.com/", "").split("/")
        if len(parts) >= 4:
            owner, repo = parts[0], parts[1]
            # Handle both /pull/ and /issues/
            number = int(parts[3])
            return owner, repo, number

    # Short form: owner/repo#123
    if "#" in ref:
        repo_part, number_str = ref.rsplit("#", 1)
        if "/" in repo_part:
            owner, repo = repo_part.split("/", 1)
            return owner, repo, int(number_str)

    raise ValueError(f"Cannot parse GitHub reference: {ref}")


def check_pr_ci(ref: str) -> tuple[bool, str | None]:
    """Check if PR CI checks have all passed.

    Args:
        ref: GitHub PR reference (URL or short form)

    Returns:
        (resolved, error) - resolved=True if all checks passed
    """
    try:
        owner, repo, number = parse_github_ref(ref)
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        result = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "state,name"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, f"gh pr checks failed: {result.stderr}"

        checks = json.loads(result.stdout)
        if not checks:
            return False, "No CI checks found"

        # All checks must pass
        all_passed = all(c.get("state") == "pass" for c in checks)
        if all_passed:
            return True, None

        # Report failing/pending checks
        not_passed = [c["name"] for c in checks if c.get("state") != "pass"]
        return False, f"Checks not passed: {', '.join(not_passed[:3])}"

    except subprocess.TimeoutExpired:
        return False, "Timeout checking PR CI"
    except json.JSONDecodeError as e:
        return False, f"Failed to parse CI status: {e}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Error checking PR CI: {e}"


def check_pr_merged(ref: str) -> tuple[bool, str | None]:
    """Check if PR has been merged.

    Args:
        ref: GitHub PR reference (URL or short form)

    Returns:
        (resolved, error) - resolved=True if PR is merged
    """
    try:
        owner, repo, number = parse_github_ref(ref)

        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "state,merged",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, f"gh pr view failed: {result.stderr}"

        pr_data = json.loads(result.stdout)
        if pr_data.get("merged"):
            return True, None
        if pr_data.get("state") == "CLOSED":
            return False, "PR closed without merge"

        return False, "PR not yet merged"

    except subprocess.TimeoutExpired:
        return False, "Timeout checking PR status"
    except json.JSONDecodeError as e:
        return False, f"Failed to parse PR status: {e}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Error checking PR: {e}"


def check_comment(ref: str, pattern: str) -> tuple[bool, str | None]:
    """Check if issue/PR has a comment matching the pattern.

    Args:
        ref: GitHub issue/PR reference
        pattern: Text pattern to search for in comments

    Returns:
        (resolved, error) - resolved=True if matching comment found
    """
    try:
        owner, repo, number = parse_github_ref(ref)

        # Get issue/PR comments
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{number}/comments",
                "--jq",
                ".[].body",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, f"gh api failed: {result.stderr}"

        comments = result.stdout.strip()
        if pattern.lower() in comments.lower():
            return True, None

        return False, f"No comment matching '{pattern}' found"

    except subprocess.TimeoutExpired:
        return False, "Timeout checking comments"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Error checking comments: {e}"


def check_time(ref: str | date) -> tuple[bool, str | None]:
    """Check if the specified time has passed.

    Args:
        ref: ISO 8601 timestamp (timezone-aware or naive). YAML loads an
            unquoted timestamp as a ``datetime`` and a bare date as a ``date``;
            both are accepted (a date means midnight, local time).

    Returns:
        (resolved, error) - resolved=True if time has passed

    Notes:
        - Timezone-aware timestamps are compared in their original timezone
        - Timezone-naive timestamps are compared against local time
    """
    try:
        if isinstance(ref, datetime):
            target_time = ref
        elif isinstance(ref, date):
            target_time = datetime.combine(ref, datetime.min.time())
        else:
            target_time = datetime.fromisoformat(ref.replace("Z", "+00:00"))

        # Handle both timezone-aware and naive datetimes
        if target_time.tzinfo is not None:
            # For aware datetimes, get current time in the same timezone
            now = datetime.now(target_time.tzinfo)
        else:
            # For naive datetimes, use local time (also naive)
            now = datetime.now()

        if now >= target_time:
            return True, None
        return False, f"Waiting until {ref}"
    except ValueError as e:
        return False, f"Invalid time format: {e}"
    except Exception as e:
        return False, f"Error checking time: {e}"


def check_condition(condition: WaitCondition) -> WaitCondition:
    """Check a single waiting condition and update its state.

    Args:
        condition: The condition to check

    Returns:
        Updated WaitCondition with resolved status
    """
    resolved = False
    error = None

    if condition.type == WaitType.PR_CI:
        resolved, error = check_pr_ci(condition.ref)
    elif condition.type == WaitType.PR_MERGED:
        resolved, error = check_pr_merged(condition.ref)
    elif condition.type == WaitType.COMMENT:
        if condition.pattern:
            resolved, error = check_comment(condition.ref, condition.pattern)
        else:
            error = "Comment check requires pattern"
    elif condition.type == WaitType.TIME:
        resolved, error = check_time(condition.ref)
    elif condition.type == WaitType.TASK:
        # Task dependencies handled by unblock.py
        pass
    elif condition.type == WaitType.UNKNOWN:
        raw_type = (condition.raw or {}).get("type")
        error = f"Unknown wait type {raw_type!r}"

    condition.resolved = resolved
    condition.error = error
    if resolved:
        condition.resolution_time = datetime.now()

    return condition


def check_all_conditions(conditions: list[WaitCondition]) -> tuple[bool, list[WaitCondition]]:
    """Check all waiting conditions.

    Args:
        conditions: List of conditions to check

    Returns:
        (all_resolved, updated_conditions)
    """
    updated = []
    for condition in conditions:
        updated.append(check_condition(condition))

    all_resolved = all(c.resolved for c in updated)
    return all_resolved, updated


def _pending_task_entries(waiting_for: object) -> list:
    """The ``type: task`` entries of a waiting_for value, as written."""
    items = waiting_for if isinstance(waiting_for, list) else [waiting_for]
    return [
        item
        for item in items
        if isinstance(item, str)
        or (isinstance(item, dict) and item.get("type", "task") == WaitType.TASK.value)
    ]


def clear_resolved_conditions(task_path: Path, expected_waiting_for: object) -> bool:
    """Drop resolved external conditions after every one of them resolved.

    Unresolved ``type: task`` entries (handled by unblock.py) are kept, and so
    is waiting_since while any remain. The write goes through ``mutate_task``
    (per-path lock, fresh read, atomic replace). If waiting_for differs from
    the snapshot that was checked, leave the file untouched and return False;
    the caller must recheck the new conditions before clearing them.
    """
    from gptodo.lifecycle import mutate_task

    def prepare(post: Any) -> list:
        if post.metadata.get("waiting_for") != expected_waiting_for:
            return []
        remaining = _pending_task_entries(post.metadata.get("waiting_for"))
        if remaining:
            return [("set", "waiting_for", remaining)]
        return [("set", "waiting_for", None), ("set", "waiting_since", None)]

    result = mutate_task(task_path, prepare=prepare, completion_effects=False)
    return result.written


def check_task_waiting(task_path: Path) -> tuple[bool, str]:
    """Check and update waiting conditions for a single task.

    Args:
        task_path: Path to the task file

    Returns:
        (resolved, message) describing the result
    """
    post = frontmatter.load(task_path)
    conditions = parse_waiting_for(post.metadata)

    if not conditions:
        return True, "No waiting conditions"

    # Only check non-task conditions (task deps handled by unblock.py)
    checkable = [c for c in conditions if c.type != WaitType.TASK]
    if not checkable:
        return True, "Only task dependencies (handled by unblock.py)"

    all_resolved, updated = check_all_conditions(checkable)

    if all_resolved:
        if not clear_resolved_conditions(task_path, post.metadata.get("waiting_for")):
            return False, "Waiting conditions changed during check - recheck required"
        return True, "External conditions resolved (task dependencies handled by unblock.py)"

    # Report status
    pending = [c for c in updated if not c.resolved]
    errors = [c.error for c in pending if c.error]
    return False, f"{len(pending)} condition(s) pending: {'; '.join(errors[:2])}"
