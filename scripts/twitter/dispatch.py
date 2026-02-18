"""Twitter dispatch handler for trusted user job requests.

When a trusted user (e.g. Erik) mentions @TimeToBuildBob with a task-like
request, this module detects it and creates a dispatch file that the
autonomous loop can pick up.

Dispatch files are written to state/dispatches/ as YAML files.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Patterns that suggest a dispatch/task request vs normal conversation
DISPATCH_PATTERNS = [
    r"\bplease\b.*\b(do|fix|add|create|implement|update|check|review|look at|work on|help with)\b",
    r"\b(can you|could you|would you)\b.*\b(do|fix|add|create|implement|update|check|review|look at|work on|help)\b",
    r"\b(go|try|start|begin|run|execute|deploy|build|test|investigate)\b",
    r"\b(file|open|submit|create)\b.*\b(issue|pr|pull request|bug|task)\b",
    r"\b(fix|address|resolve|handle|tackle)\b.*\b(bug|issue|error|problem|regression)\b",
]

# Patterns that suggest normal conversation (not a dispatch)
CONVERSATION_PATTERNS = [
    r"^(thanks|thank you|nice|great|good|lol|haha|yeah|yes|no|ok|okay)\b",
    r"^(what do you think|how do you feel|do you agree)",
    r"\?$",  # Pure questions are usually not dispatches
]


@dataclass
class DispatchRequest:
    """A parsed dispatch request from a trusted user."""

    tweet_id: str
    author: str
    text: str
    task_summary: str
    created_at: str
    tweet_url: str


def detect_dispatch(tweet_text: str, author_username: str, is_trusted: bool) -> bool:
    """Check if a tweet from a trusted user looks like a dispatch request.

    Args:
        tweet_text: The tweet text (with @mention removed)
        author_username: The author's username
        is_trusted: Whether the author is a trusted user

    Returns:
        True if this looks like a dispatch request
    """
    if not is_trusted:
        return False

    # Remove @mentions from text for pattern matching
    clean_text = re.sub(r"@\w+", "", tweet_text).strip().lower()

    # Skip very short texts
    if len(clean_text) < 10:
        return False

    # Check for conversation patterns first (these override dispatch patterns)
    for pattern in CONVERSATION_PATTERNS:
        if re.search(pattern, clean_text, re.IGNORECASE):
            return False

    # Check for dispatch patterns
    for pattern in DISPATCH_PATTERNS:
        if re.search(pattern, clean_text, re.IGNORECASE):
            return True

    return False


def extract_task_summary(tweet_text: str) -> str:
    """Extract a clean task summary from tweet text.

    Removes @mentions and cleans up the text for use as a task description.
    """
    # Remove @mentions
    summary = re.sub(r"@\w+", "", tweet_text).strip()
    # Collapse whitespace
    summary = re.sub(r"\s+", " ", summary)
    return summary


def create_dispatch(
    tweet_id: str,
    author: str,
    tweet_text: str,
    agent_dir: Path,
) -> Path:
    """Create a dispatch file for the autonomous loop to pick up.

    Args:
        tweet_id: The tweet ID
        author: The author username
        tweet_text: The full tweet text
        agent_dir: The agent workspace directory

    Returns:
        Path to the created dispatch file
    """
    dispatch_dir = agent_dir / "state" / "dispatches"
    dispatch_dir.mkdir(parents=True, exist_ok=True)

    task_summary = extract_task_summary(tweet_text)
    now = datetime.now(timezone.utc)
    tweet_url = f"https://twitter.com/{author}/status/{tweet_id}"

    dispatch = {
        "tweet_id": tweet_id,
        "author": author,
        "text": tweet_text,
        "task_summary": task_summary,
        "tweet_url": tweet_url,
        "created_at": now.isoformat(),
        "status": "pending",
    }

    filename = f"dispatch_{now.strftime('%Y%m%d_%H%M%S')}_{tweet_id}.yml"
    filepath = dispatch_dir / filename

    with open(filepath, "w") as f:
        yaml.dump(dispatch, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Created dispatch file: {filepath}")
    return filepath


def get_pending_dispatches(agent_dir: Path) -> list[dict]:
    """Get all pending dispatch files.

    Args:
        agent_dir: The agent workspace directory

    Returns:
        List of dispatch dictionaries
    """
    dispatch_dir = agent_dir / "state" / "dispatches"
    if not dispatch_dir.exists():
        return []

    dispatches = []
    for f in sorted(dispatch_dir.glob("dispatch_*.yml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
            if data and data.get("status") == "pending":
                data["_filepath"] = str(f)
                dispatches.append(data)

    return dispatches


def mark_dispatch_done(filepath: str | Path, result: str = "completed") -> None:
    """Mark a dispatch as processed.

    Args:
        filepath: Path to the dispatch file
        result: Result status (completed, failed, skipped)
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return

    with open(filepath) as f:
        data = yaml.safe_load(f)

    data["status"] = result
    data["processed_at"] = datetime.now(timezone.utc).isoformat()

    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
