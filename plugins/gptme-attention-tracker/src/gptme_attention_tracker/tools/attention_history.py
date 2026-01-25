"""
Attention history tracking - queryable record of context usage.

Records what files/lessons were in context during each turn and session.
Enables:
- Meta-learning: Which context led to which outcomes
- Pattern analysis: What files activate together
- Debugging: Why certain files were/weren't in context
- Optimization: Identify underutilized or over-included files
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from gptme.tools.base import ToolSpec

logger = logging.getLogger(__name__)

# History file location
HISTORY_FILE = Path(".gptme/attention_history.jsonl")


@dataclass
class HistoryEntry:
    """A single history entry recording context state."""

    timestamp: str
    session_id: str
    turn_number: int
    hot_files: list[str]
    warm_files: list[str]
    cold_files: list[str]
    activated_keywords: list[str]
    message_preview: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "hot_files": self.hot_files,
            "warm_files": self.warm_files,
            "cold_files": self.cold_files,
            "activated_keywords": self.activated_keywords,
            "message_preview": self.message_preview,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        """Create from dict."""
        return cls(
            timestamp=data["timestamp"],
            session_id=data["session_id"],
            turn_number=data["turn_number"],
            hot_files=data.get("hot_files", []),
            warm_files=data.get("warm_files", []),
            cold_files=data.get("cold_files", []),
            activated_keywords=data.get("activated_keywords", []),
            message_preview=data.get("message_preview", ""),
            metadata=data.get("metadata", {}),
        )


# Session tracking
_current_session_id: str | None = None


def _get_session_id() -> str:
    """Get or create current session ID."""
    global _current_session_id
    if _current_session_id is None:
        _current_session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _current_session_id


def _ensure_history_file() -> None:
    """Ensure history file exists."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not HISTORY_FILE.exists():
        HISTORY_FILE.touch()


def record_turn(
    turn_number: int,
    hot_files: list[str],
    warm_files: list[str] | None = None,
    cold_files: list[str] | None = None,
    activated_keywords: list[str] | None = None,
    message_preview: str = "",
    metadata: dict | None = None,
) -> str:
    """
    Record context state for a turn.

    Args:
        turn_number: The turn number in the current session
        hot_files: Files that were fully included (HOT tier)
        warm_files: Files that were partially included (WARM tier)
        cold_files: Files that were excluded (COLD tier)
        activated_keywords: Keywords that triggered file activation
        message_preview: Preview of the user message (first 100 chars)
        metadata: Additional metadata to record

    Returns:
        Status message

    Example:
        record_turn(
            turn_number=5,
            hot_files=["lessons/workflow/git-workflow.md"],
            warm_files=["lessons/tools/shell.md"],
            activated_keywords=["git", "commit"],
            message_preview="How do I commit changes..."
        )
    """
    _ensure_history_file()

    entry = HistoryEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        session_id=_get_session_id(),
        turn_number=turn_number,
        hot_files=hot_files,
        warm_files=warm_files or [],
        cold_files=cold_files or [],
        activated_keywords=activated_keywords or [],
        message_preview=message_preview[:100] if message_preview else "",
        metadata=metadata or {},
    )

    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry.to_dict()) + "\n")

    return f"Recorded turn {turn_number}: {len(hot_files)} HOT, {len(warm_files or [])} WARM files"


def query_session(session_id: str | None = None) -> list[dict]:
    """
    Query history for a specific session.

    Args:
        session_id: Session ID to query (default: current session)

    Returns:
        List of history entries for the session
    """
    _ensure_history_file()
    target_session = session_id or _get_session_id()
    entries = []

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == target_session:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue

    return entries


def query_file(file_path: str, limit: int = 50) -> dict:
    """
    Query history for a specific file.

    Returns when and how often a file appeared in context.

    Args:
        file_path: The file path to query
        limit: Maximum entries to return

    Returns:
        Dict with file statistics and recent appearances
    """
    _ensure_history_file()
    hot_count = 0
    warm_count = 0
    cold_count = 0
    sessions: set[str] = set()
    recent: list[dict] = []

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    in_hot = file_path in entry.get("hot_files", [])
                    in_warm = file_path in entry.get("warm_files", [])
                    in_cold = file_path in entry.get("cold_files", [])

                    if in_hot:
                        hot_count += 1
                        sessions.add(entry.get("session_id", ""))
                        if len(recent) < limit:
                            recent.append(
                                {
                                    "timestamp": entry.get("timestamp"),
                                    "session_id": entry.get("session_id"),
                                    "turn": entry.get("turn_number"),
                                    "tier": "HOT",
                                }
                            )
                    elif in_warm:
                        warm_count += 1
                        sessions.add(entry.get("session_id", ""))
                        if len(recent) < limit:
                            recent.append(
                                {
                                    "timestamp": entry.get("timestamp"),
                                    "session_id": entry.get("session_id"),
                                    "turn": entry.get("turn_number"),
                                    "tier": "WARM",
                                }
                            )
                    elif in_cold:
                        cold_count += 1
                except json.JSONDecodeError:
                    continue

    return {
        "file": file_path,
        "hot_count": hot_count,
        "warm_count": warm_count,
        "cold_count": cold_count,
        "total_appearances": hot_count + warm_count,
        "sessions_appeared": len(sessions),
        "recent_appearances": recent[-10:],  # Last 10
    }


def query_coactivation(limit: int = 20) -> list[dict]:
    """
    Analyze which files frequently appear together.

    Returns pairs of files that often co-occur in HOT tier.

    Args:
        limit: Maximum pairs to return

    Returns:
        List of file pairs with co-occurrence counts
    """
    _ensure_history_file()
    cooccurrence: dict[tuple[str, str], int] = {}

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    hot_files = entry.get("hot_files", [])

                    # Count pairs
                    for i, file1 in enumerate(hot_files):
                        for file2 in hot_files[i + 1 :]:
                            pair = tuple(sorted([file1, file2]))
                            cooccurrence[pair] = cooccurrence.get(pair, 0) + 1
                except json.JSONDecodeError:
                    continue

    # Sort by count descending
    sorted_pairs = sorted(cooccurrence.items(), key=lambda x: x[1], reverse=True)

    return [
        {"file1": pair[0], "file2": pair[1], "count": count}
        for pair, count in sorted_pairs[:limit]
    ]


def query_keyword_effectiveness(limit: int = 20) -> list[dict]:
    """
    Analyze keyword activation patterns.

    Shows which keywords most frequently trigger file activations.

    Args:
        limit: Maximum keywords to return

    Returns:
        List of keywords with activation counts
    """
    _ensure_history_file()
    keyword_counts: dict[str, int] = {}
    keyword_files: dict[str, set[str]] = {}

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    keywords = entry.get("activated_keywords", [])
                    hot_files = entry.get("hot_files", [])

                    for kw in keywords:
                        keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                        if kw not in keyword_files:
                            keyword_files[kw] = set()
                        keyword_files[kw].update(hot_files)
                except json.JSONDecodeError:
                    continue

    # Sort by count descending
    sorted_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)

    return [
        {
            "keyword": kw,
            "activation_count": count,
            "files_activated": len(keyword_files.get(kw, set())),
        }
        for kw, count in sorted_keywords[:limit]
    ]


def get_summary(last_n_sessions: int = 5) -> dict:
    """
    Get summary statistics from attention history.

    Args:
        last_n_sessions: Number of recent sessions to analyze

    Returns:
        Summary statistics
    """
    _ensure_history_file()
    sessions: dict[str, list] = {}
    total_turns = 0
    total_hot_files = 0
    total_warm_files = 0

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    session_id = entry.get("session_id", "unknown")
                    if session_id not in sessions:
                        sessions[session_id] = []
                    sessions[session_id].append(entry)
                    total_turns += 1
                    total_hot_files += len(entry.get("hot_files", []))
                    total_warm_files += len(entry.get("warm_files", []))
                except json.JSONDecodeError:
                    continue

    # Get recent sessions
    session_ids = sorted(sessions.keys(), reverse=True)[:last_n_sessions]

    return {
        "total_sessions": len(sessions),
        "total_turns": total_turns,
        "avg_hot_files_per_turn": round(total_hot_files / max(total_turns, 1), 2),
        "avg_warm_files_per_turn": round(total_warm_files / max(total_turns, 1), 2),
        "recent_sessions": [
            {
                "session_id": sid,
                "turns": len(sessions[sid]),
                "started": sessions[sid][0].get("timestamp") if sessions[sid] else None,
            }
            for sid in session_ids
        ],
        "current_session": _get_session_id(),
    }


def find_underutilized(min_appearances: int = 10) -> list[dict]:
    """
    Find files that are tracked but rarely appear in HOT tier.

    Useful for identifying files with poor keyword matching.

    Args:
        min_appearances: Minimum total appearances to consider

    Returns:
        List of underutilized files
    """
    _ensure_history_file()
    file_stats: dict[str, dict] = {}

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    for f_path in entry.get("hot_files", []):
                        if f_path not in file_stats:
                            file_stats[f_path] = {"hot": 0, "warm": 0, "cold": 0}
                        file_stats[f_path]["hot"] += 1
                    for f_path in entry.get("warm_files", []):
                        if f_path not in file_stats:
                            file_stats[f_path] = {"hot": 0, "warm": 0, "cold": 0}
                        file_stats[f_path]["warm"] += 1
                    for f_path in entry.get("cold_files", []):
                        if f_path not in file_stats:
                            file_stats[f_path] = {"hot": 0, "warm": 0, "cold": 0}
                        file_stats[f_path]["cold"] += 1
                except json.JSONDecodeError:
                    continue

    # Find files with low HOT ratio
    underutilized = []
    for f_path, stats in file_stats.items():
        total = stats["hot"] + stats["warm"] + stats["cold"]
        if total >= min_appearances:
            hot_ratio = stats["hot"] / total if total > 0 else 0
            if hot_ratio < 0.2:  # Less than 20% in HOT tier
                underutilized.append(
                    {
                        "file": f_path,
                        "hot_count": stats["hot"],
                        "total_appearances": total,
                        "hot_ratio": round(hot_ratio, 3),
                    }
                )

    # Sort by hot_ratio ascending (most underutilized first)
    underutilized.sort(key=lambda x: x["hot_ratio"])
    return underutilized[:20]


def clear_history(older_than_days: int | None = None) -> str:
    """
    Clear attention history.

    Args:
        older_than_days: If specified, only clear entries older than N days

    Returns:
        Status message
    """
    _ensure_history_file()

    if older_than_days is None:
        # Clear all
        with open(HISTORY_FILE, "w") as f:
            pass
        return "Cleared all attention history"

    # Clear entries older than N days
    cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
    kept = []

    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(
                        entry["timestamp"].replace("Z", "+00:00")
                    )
                    if ts.timestamp() >= cutoff:
                        kept.append(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

    with open(HISTORY_FILE, "w") as f:
        f.writelines(kept)

    return (
        f"Cleared history older than {older_than_days} days, kept {len(kept)} entries"
    )


def start_new_session(session_id: str | None = None) -> str:
    """
    Start a new session for tracking.

    Args:
        session_id: Custom session ID (default: auto-generated)

    Returns:
        The new session ID
    """
    global _current_session_id
    _current_session_id = session_id or datetime.now(timezone.utc).strftime(
        "%Y%m%d_%H%M%S"
    )
    return f"Started new session: {_current_session_id}"


# Tool specification for gptme
tool = ToolSpec(
    name="attention_history",
    desc="Track and query attention history - what was in context when",
    instructions="""
Use this tool to track and analyze what files/lessons were in context.

**Features:**
- Record context state each turn
- Query file appearance history
- Analyze co-activation patterns (which files appear together)
- Find underutilized files (poor keyword matching)
- Track keyword effectiveness

**Usage Pattern:**

1. Record each turn after attention routing:
```python
record_turn(
    turn_number=5,
    hot_files=["lessons/workflow/git-workflow.md"],
    warm_files=["lessons/tools/shell.md"],
    activated_keywords=["git", "commit"],
    message_preview="How do I commit..."
)
```

2. Query history for analysis:
```python
# Check how often a file appears
stats = query_file("lessons/workflow/git-workflow.md")

# Find co-occurring files (for co-activation setup)
pairs = query_coactivation()

# Find underutilized files
underused = find_underutilized()
```

**Use Cases:**
- Meta-learning: Correlate context with outcomes
- Optimization: Identify files with poor keyword matching
- Debugging: Why was/wasn't a file in context?
- Co-activation: Discover related files to link
    """,
    examples="""
### Record Context State

> User: I just processed a turn with the attention router
> Assistant: I'll record the context state for analysis.
```ipython
record_turn(
    turn_number=5,
    hot_files=["lessons/workflow/git-workflow.md", "TASKS.md"],
    warm_files=["lessons/tools/shell.md"],
    activated_keywords=["git", "branch"],
    message_preview="How do I create a new branch?"
)
```

### Query File History

> User: How often is git-workflow.md in context?
> Assistant: Let me check the history for that file.
```ipython
stats = query_file("lessons/workflow/git-workflow.md")
print(f"HOT: {stats['hot_count']} times, WARM: {stats['warm_count']} times")
print(f"Appeared in {stats['sessions_appeared']} sessions")
```

### Find Co-activation Patterns

> User: What files often appear together?
> Assistant: I'll analyze co-activation patterns.
```ipython
pairs = query_coactivation(limit=10)
for p in pairs:
    print(f"{p['file1']} <-> {p['file2']}: {p['count']} times")
```

### Find Underutilized Files

> User: Are there files that should be included more often?
> Assistant: Let me find underutilized files.
```ipython
underused = find_underutilized()
for f in underused:
    print(f"{f['file']}: {f['hot_ratio']:.1%} HOT rate")
```

### Get Summary

> User: Give me a summary of context history
> Assistant: Here's the attention history summary.
```ipython
summary = get_summary()
print(f"Total sessions: {summary['total_sessions']}")
print(f"Avg HOT files per turn: {summary['avg_hot_files_per_turn']}")
```
    """,
    functions=[
        record_turn,
        query_session,
        query_file,
        query_coactivation,
        query_keyword_effectiveness,
        get_summary,
        find_underutilized,
        clear_history,
        start_new_session,
    ],
)
