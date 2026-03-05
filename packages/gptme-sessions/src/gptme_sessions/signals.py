"""Extract productivity signals from gptme trajectory files (conversation.jsonl).

Provides grounded reward signals for Thompson sampling and session analytics
by analyzing actual transcripts rather than self-reported journals.

Journals cannot be relied on for consistent structure. Trajectories are
ground truth: every tool call, every error, every file write is recorded.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


def parse_trajectory(jsonl_path: Path) -> list[dict]:
    """Parse a conversation.jsonl file into a list of messages."""
    msgs = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return msgs


def _extract_path_from_args(args_str: str) -> str | None:
    """Extract the 'path' field from a tool call's JSON args string."""
    m = re.search(r'"path"\s*:\s*"([^"]+)"', args_str)
    return m.group(1) if m else None


def extract_signals(msgs: list[dict]) -> dict:
    """Extract productivity signals from parsed trajectory messages.

    Returns a dict with counts and extracted deliverables — no LLM needed.

    Tool call format in gptme trajectories:
      @tool_name(call_id): {"arg": "value", ...}
    Tool results are system messages with timestamps.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []

    # Track recent (tool, path) pairs for retry detection
    recent_sigs: list[str] = []

    for msg in msgs:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        ts_str = msg.get("timestamp", "")

        # Parse timestamps for duration
        if ts_str:
            try:
                from datetime import timezone as _tz

                ts_clean = ts_str.replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_clean)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                timestamps.append(ts)
            except ValueError:
                pass

        if role == "assistant":
            # Find all @tool_name(call_id): JSON_ARGS blocks.
            blocks = re.split(r"(?=\n@\w+\()", "\n" + content)
            for block in blocks:
                m = re.match(r"\n@(\w+)\(([^)]+)\):\s*(.*)", block, re.DOTALL)
                if not m:
                    continue
                tool, _call_id, args_str = m.group(1), m.group(2), m.group(3)
                tool_calls[tool] = tool_calls.get(tool, 0) + 1

                if tool in ("save", "write", "patch", "edit"):
                    path = _extract_path_from_args(args_str)
                    if path:
                        if "/journal/" not in path:
                            file_writes.append(path)
                    else:
                        file_writes.append(f"<{tool}>")

                if tool in ("save", "write", "patch", "edit"):
                    path = _extract_path_from_args(args_str)
                    if path:
                        sig = f"{tool}:{path}"
                        if sig in recent_sigs:
                            retry_candidates.append(tool)
                        recent_sigs.append(sig)
                        if len(recent_sigs) > 20:
                            recent_sigs.pop(0)

        elif role == "system" and ts_str:
            content_stripped = content.strip()

            # Error detection
            if (
                content_stripped.startswith("Error")
                or "Error during execution:" in content[:100]
                or "error:" in content[:80].lower()
                and not content_stripped.startswith("Ran command:")
            ):
                error_count += 1

            # Git commit detection from shell output
            commit_match = re.search(
                r"\[(?:master|main|[a-z0-9_/-]+)\s+([0-9a-f]{7,12})\]\s+(.+?)(?:\n|$)",
                content,
            )
            if commit_match:
                commit_hash = commit_match.group(1)
                commit_msg = commit_match.group(2).strip()
                git_commits.append(f"{commit_msg} ({commit_hash})")

    # Session duration
    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    # Combine deliverables: git commits + distinct file writes
    deliverables = list(dict.fromkeys(git_commits + file_writes))
    retry_count = len(retry_candidates)

    return {
        "tool_calls": tool_calls,
        "error_count": error_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "session_duration_s": duration_s,
        "retry_count": retry_count,
        "deliverables": deliverables,
    }


def grade_signals(signals: dict) -> float:
    """Compute a graded reward (0.0-1.0) from trajectory signals.

    Based on ground-truth evidence from the transcript:
    - Git commits (strongest signal)
    - File writes / patches
    - Error rate penalty
    - Retry penalty
    """
    commits = len(signals["git_commits"])
    writes = len(signals["file_writes"])
    errors = signals["error_count"]
    retries = signals["retry_count"]
    total_tools = sum(signals["tool_calls"].values())
    has_complete = "complete" in signals["tool_calls"]

    if commits == 0 and writes == 0:
        reward = 0.25
    elif commits == 0:
        if writes >= 3:
            reward = 0.55
        else:
            reward = 0.40
    elif commits == 1:
        reward = 0.60
    elif commits == 2:
        reward = 0.70
    elif commits == 3:
        reward = 0.78
    else:
        reward = min(0.92, 0.80 + 0.03 * (commits - 4))

    if total_tools > 0:
        error_rate = errors / total_tools
        if error_rate > 0.15:
            reward -= 0.10
        elif error_rate > 0.05:
            reward -= 0.05

    if retries >= 3:
        reward -= 0.08
    elif retries >= 1:
        reward -= 0.03

    if has_complete:
        reward += 0.03

    return max(0.0, min(1.0, reward))


def is_productive(signals: dict) -> bool:
    """Quick binary productive/noop classification from trajectory signals."""
    return bool(signals["git_commits"] or len(signals["file_writes"]) >= 2)


def extract_from_path(jsonl_path: Path) -> dict:
    """Parse trajectory and return signals + grade in one call."""
    msgs = parse_trajectory(jsonl_path)
    signals = extract_signals(msgs)
    grade = grade_signals(signals)
    return {
        **signals,
        "productive": is_productive(signals),
        "grade": round(grade, 4),
    }
