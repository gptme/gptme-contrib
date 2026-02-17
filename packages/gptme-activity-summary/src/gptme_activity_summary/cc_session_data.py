"""
Parse Claude Code session logs to extract session metadata.

Scans `~/.claude/projects/` for session JSONL files and extracts
model usage, token counts, and session durations.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from .session_data import SessionInfo, SessionStats, _aggregate_session

logger = logging.getLogger(__name__)

DEFAULT_CC_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _get_cc_projects_dir() -> Path:
    """Get Claude Code projects directory from env or default."""
    env_dir = os.environ.get("CLAUDE_HOME")
    if env_dir:
        return Path(env_dir) / "projects"
    return DEFAULT_CC_PROJECTS_DIR


def _decode_project_path(encoded: str) -> str:
    """Decode a CC project directory name to the original path.

    CC encodes paths by replacing '/' with '-', e.g.:
        -home-bob-bob -> /home/bob/bob
    """
    if not encoded.startswith("-"):
        return encoded
    # Replace leading '-' with '/', then remaining '-' with '/'
    return encoded.replace("-", "/")


def _session_date_from_first_line(jsonl_path: Path) -> date | None:
    """Quick date extraction: read first lines to find a timestamp.

    Returns the date of the session without fully parsing the file.
    """
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        return ts.date()
                    except (ValueError, TypeError):
                        continue
    except (OSError, PermissionError) as e:
        logger.debug("Failed to read %s: %s", jsonl_path, e)
    return None


def _parse_cc_session(jsonl_path: Path) -> SessionInfo:
    """Parse a single Claude Code session JSONL file.

    Extracts model, token usage, duration, and message count from
    assistant messages in the session log.
    """
    info = SessionInfo(name=jsonl_path.stem, harness="claude-code")

    model = ""
    input_tokens = 0
    output_tokens = 0
    message_count = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    is_bypass = False

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")

                # Track timestamps from any entry
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_timestamp is None or ts < first_timestamp:
                            first_timestamp = ts
                        if last_timestamp is None or ts > last_timestamp:
                            last_timestamp = ts
                    except (ValueError, TypeError):
                        pass

                # Check permission mode on user messages
                if entry.get("permissionMode") == "bypassPermissions":
                    is_bypass = True

                # Extract project path for workspace
                cwd = entry.get("cwd")
                if cwd and not info.workspace:
                    info.workspace = cwd

                if entry_type not in ("assistant", "user"):
                    continue

                message_count += 1

                if entry_type != "assistant":
                    continue

                # Extract model and usage from assistant messages
                msg = entry.get("message", {})
                if not model and msg.get("model"):
                    model = msg["model"]

                usage = msg.get("usage", {})
                if usage:
                    input_tokens += usage.get("input_tokens", 0) or 0
                    output_tokens += usage.get("output_tokens", 0) or 0

    except (OSError, PermissionError) as e:
        logger.debug("Failed to parse %s: %s", jsonl_path, e)

    info.model = model
    info.input_tokens = input_tokens
    info.output_tokens = output_tokens
    info.message_count = message_count
    info.cost = 0.0  # Subscription model, no per-token cost
    info.interactive = not is_bypass

    if first_timestamp and last_timestamp:
        duration = (last_timestamp - first_timestamp).total_seconds()
        info.duration_seconds = max(0.0, duration)

    return info


def fetch_cc_session_stats_range(
    start: date,
    end: date,
    cc_dir: Path | None = None,
) -> SessionStats:
    """Fetch aggregated CC session stats for a date range.

    Scans all project directories under ~/.claude/projects/ for session
    JSONL files, filters by date, and aggregates stats.

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)
        cc_dir: Override projects directory (mainly for testing)

    Returns:
        SessionStats for the range. Returns empty stats if dir doesn't exist.
    """
    if cc_dir is None:
        cc_dir = _get_cc_projects_dir()

    stats = SessionStats(start_date=start, end_date=end)

    if not cc_dir.exists():
        logger.debug("CC projects directory does not exist: %s", cc_dir)
        return stats

    try:
        for project_dir in sorted(cc_dir.iterdir()):
            if not project_dir.is_dir():
                continue

            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                # Quick date check — skip files outside range
                session_date = _session_date_from_first_line(jsonl_file)
                if session_date is None:
                    continue
                if not (start <= session_date <= end):
                    continue

                # Full parse
                session_info = _parse_cc_session(jsonl_file)

                # Set workspace from decoded project dir if not set from cwd
                if not session_info.workspace:
                    session_info.workspace = _decode_project_path(project_dir.name)

                # Aggregate
                _aggregate_session(stats, session_info)

    except PermissionError:
        logger.debug("Permission denied reading CC projects directory: %s", cc_dir)

    return stats
