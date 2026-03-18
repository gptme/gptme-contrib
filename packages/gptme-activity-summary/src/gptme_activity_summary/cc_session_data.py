"""
Parse Claude Code session logs to extract session metadata.

Delegates directory scanning and token extraction to gptme-sessions,
keeping only CC-specific metadata extraction and aggregation here.
"""

import logging
from datetime import datetime
from pathlib import Path

from gptme_sessions.discovery import decode_cc_project_path, discover_cc_sessions
from gptme_sessions.signals import extract_usage_cc, parse_trajectory

from .session_data import SessionInfo, SessionStats, _aggregate_session

logger = logging.getLogger(__name__)


def _extract_cc_metadata(msgs: list[dict]) -> dict:
    """Extract CC-specific metadata not covered by gptme-sessions.

    Returns workspace, interactive flag, message count, steps, and duration.
    """
    workspace = ""
    is_bypass = False
    message_count = 0
    assistant_turns = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for entry in msgs:
        # Timestamps from any entry
        ts_str = entry.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            except (ValueError, TypeError):
                pass

        # Workspace from cwd field
        cwd = entry.get("cwd")
        if cwd and not workspace:
            workspace = cwd

        # Permission mode
        if entry.get("permissionMode") == "bypassPermissions":
            is_bypass = True

        # Count user + assistant messages
        entry_type = entry.get("type")
        if entry_type in ("assistant", "user"):
            message_count += 1
        if entry_type == "assistant":
            assistant_turns += 1

    duration = 0.0
    if first_ts and last_ts:
        duration = max(0.0, (last_ts - first_ts).total_seconds())

    return {
        "workspace": workspace,
        "interactive": not is_bypass,
        "message_count": message_count,
        "steps": assistant_turns,
        "duration_seconds": duration,
    }


def fetch_cc_session_stats_range(
    start,
    end,
    cc_dir: Path | None = None,
) -> SessionStats:
    """Fetch aggregated CC session stats for a date range.

    Uses gptme-sessions for directory discovery and token extraction.
    Handles CC-specific metadata (workspace, interactive, timestamps) locally.
    """
    stats = SessionStats(start_date=start, end_date=end)

    for jsonl_file in discover_cc_sessions(start, end, cc_dir):
        msgs = parse_trajectory(jsonl_file)

        # Token usage via gptme-sessions
        usage = extract_usage_cc(msgs)

        # CC-specific metadata
        meta = _extract_cc_metadata(msgs)

        # Skip empty sessions (no assistant response — just initialization artifacts)
        if meta["steps"] == 0:
            continue

        # Include cache tokens on input side for accurate totals
        input_tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_tokens", 0)
            + usage.get("cache_creation_tokens", 0)
        )

        info = SessionInfo(
            name=jsonl_file.stem,
            harness="claude-code",
            model=usage.get("model") or "",
            workspace=meta["workspace"],
            message_count=meta["message_count"],
            steps=meta["steps"],
            input_tokens=input_tokens,
            output_tokens=usage.get("output_tokens", 0),
            cost=0.0,  # Subscription model, no per-token cost
            duration_seconds=meta["duration_seconds"],
            interactive=meta["interactive"],
        )

        # Fall back to decoded project dir for workspace
        if not info.workspace:
            info.workspace = decode_cc_project_path(jsonl_file.parent.name)

        _aggregate_session(stats, info)

    return stats
