"""
Parse gptme conversation logs to extract session metadata.

Scans `~/.local/share/gptme/logs/` for session directories matching a date range
and extracts model usage, token counts, costs, and session durations.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOGS_DIR = Path.home() / ".local" / "share" / "gptme" / "logs"


@dataclass
class SessionInfo:
    """Metadata for a single gptme session."""

    name: str
    model: str = ""
    workspace: str = ""
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0
    interactive: bool = True


@dataclass
class SessionStats:
    """Aggregated session stats for a date range."""

    start_date: date
    end_date: date
    session_count: int = 0
    models_used: dict[str, int] = field(default_factory=dict)  # model -> count
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    total_duration_seconds: float = 0.0
    sessions: list[SessionInfo] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


def _get_logs_dir() -> Path:
    """Get gptme logs directory from env or default."""
    env_dir = os.environ.get("GPTME_LOGS_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_LOGS_DIR


def _parse_config_toml(config_path: Path) -> dict:
    """Parse a session's config.toml file."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        with open(config_path, "rb") as f:
            result: dict = tomllib.load(f)
            return result
    except Exception as e:
        logger.debug("Failed to parse %s: %s", config_path, e)
        return {}


def _parse_conversation_jsonl(conv_path: Path) -> dict:
    """
    Parse a conversation.jsonl file to extract message stats.

    Returns dict with: message_count, input_tokens, output_tokens, cost,
    duration_seconds, first_timestamp, last_timestamp
    """
    stats: dict = {
        "message_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": 0.0,
        "first_timestamp": None,
        "last_timestamp": None,
    }

    try:
        with open(conv_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                stats["message_count"] += 1

                # Extract timestamp
                ts_str = msg.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if stats["first_timestamp"] is None or ts < stats["first_timestamp"]:
                            stats["first_timestamp"] = ts
                        if stats["last_timestamp"] is None or ts > stats["last_timestamp"]:
                            stats["last_timestamp"] = ts
                    except (ValueError, TypeError):
                        pass

                # Extract token usage if present
                usage = msg.get("usage") or {}
                stats["input_tokens"] += (
                    usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0
                )
                stats["output_tokens"] += (
                    usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0
                )
                stats["cost"] += usage.get("cost", 0.0) or 0.0

    except Exception as e:
        logger.debug("Failed to parse %s: %s", conv_path, e)

    # Calculate duration
    if stats["first_timestamp"] and stats["last_timestamp"]:
        duration = (stats["last_timestamp"] - stats["first_timestamp"]).total_seconds()
        stats["duration_seconds"] = max(0.0, duration)
    else:
        stats["duration_seconds"] = 0.0

    return stats


def _session_matches_date(session_name: str, target_date: date) -> bool:
    """Check if a session directory name matches a target date (starts with YYYY-MM-DD)."""
    date_str = target_date.isoformat()
    return session_name.startswith(date_str)


def _session_matches_range(session_name: str, start: date, end: date) -> bool:
    """Check if a session directory name falls within a date range."""
    # Session names start with YYYY-MM-DD or similar date prefix
    try:
        session_date_str = session_name[:10]
        session_date = date.fromisoformat(session_date_str)
        return start <= session_date <= end
    except (ValueError, IndexError):
        return False


def fetch_session_stats(
    target_date: date,
    logs_dir: Path | None = None,
) -> SessionStats:
    """
    Fetch session stats for a single date.

    Args:
        target_date: The date to get stats for
        logs_dir: Override logs directory (mainly for testing)

    Returns:
        SessionStats for the date. Returns empty stats if logs dir doesn't exist.
    """
    return fetch_session_stats_range(target_date, target_date, logs_dir)


def fetch_session_stats_range(
    start: date,
    end: date,
    logs_dir: Path | None = None,
) -> SessionStats:
    """
    Fetch aggregated session stats for a date range.

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)
        logs_dir: Override logs directory (mainly for testing)

    Returns:
        SessionStats for the range. Returns empty stats if logs dir doesn't exist.
    """
    if logs_dir is None:
        logs_dir = _get_logs_dir()

    stats = SessionStats(start_date=start, end_date=end)

    if not logs_dir.exists():
        logger.debug("Logs directory does not exist: %s", logs_dir)
        return stats

    # Scan for session directories matching the date range
    try:
        for session_dir in sorted(logs_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            if not _session_matches_range(session_dir.name, start, end):
                continue

            session_info = SessionInfo(name=session_dir.name)

            # Parse config.toml if it exists
            config_path = session_dir / "config.toml"
            if config_path.exists():
                config = _parse_config_toml(config_path)
                session_info.model = config.get("model", "")
                session_info.workspace = config.get("workspace", "")
                session_info.interactive = config.get("interactive", True)

            # Parse conversation.jsonl if it exists
            conv_path = session_dir / "conversation.jsonl"
            if conv_path.exists():
                conv_stats = _parse_conversation_jsonl(conv_path)
                session_info.message_count = conv_stats["message_count"]
                session_info.input_tokens = conv_stats["input_tokens"]
                session_info.output_tokens = conv_stats["output_tokens"]
                session_info.cost = conv_stats["cost"]
                session_info.duration_seconds = conv_stats["duration_seconds"]

            # Aggregate
            stats.session_count += 1
            stats.sessions.append(session_info)

            if session_info.model:
                stats.models_used[session_info.model] = (
                    stats.models_used.get(session_info.model, 0) + 1
                )

            stats.total_input_tokens += session_info.input_tokens
            stats.total_output_tokens += session_info.output_tokens
            stats.total_cost += session_info.cost
            stats.total_duration_seconds += session_info.duration_seconds

    except PermissionError:
        logger.debug("Permission denied reading logs directory: %s", logs_dir)

    return stats


def format_sessions_for_prompt(stats: SessionStats) -> str:
    """
    Format session stats as markdown for injection into LLM prompts.

    Returns empty string if no session data.
    """
    if stats.session_count == 0:
        return ""

    lines: list[str] = []
    lines.append("## gptme Session Data (Real Data)")
    lines.append(f"Period: {stats.start_date.isoformat()} to {stats.end_date.isoformat()}")
    lines.append(f"- **Sessions**: {stats.session_count}")

    if stats.models_used:
        models_str = ", ".join(
            f"{model} ({count}x)"
            for model, count in sorted(stats.models_used.items(), key=lambda x: -x[1])
        )
        lines.append(f"- **Models used**: {models_str}")

    if stats.total_tokens > 0:
        lines.append(
            f"- **Total tokens**: {stats.total_tokens:,} (input: {stats.total_input_tokens:,}, output: {stats.total_output_tokens:,})"
        )

    if stats.total_cost > 0:
        lines.append(f"- **Total cost**: ${stats.total_cost:.2f}")

    if stats.total_duration_seconds > 0:
        hours = stats.total_duration_seconds / 3600
        lines.append(f"- **Total session time**: {hours:.1f}h")

    lines.append("")
    return "\n".join(lines)
