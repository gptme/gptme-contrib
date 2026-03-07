"""Discover session files across agent harnesses.

Scans known session directories for gptme, Claude Code, Codex CLI,
and Copilot CLI, filtering by date range. This replaces the directory
scanning logic previously duplicated in gptme-activity-summary's
session_data.py and cc_session_data.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .record import SessionRecord

logger = logging.getLogger(__name__)

DEFAULT_GPTME_LOGS_DIR = Path.home() / ".local" / "share" / "gptme" / "logs"
DEFAULT_CC_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_COPILOT_STATE_DIR = Path.home() / ".copilot" / "session-state"


def _get_gptme_logs_dir() -> Path:
    """Get gptme logs directory from env or default."""
    env_dir = os.environ.get("GPTME_LOGS_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_GPTME_LOGS_DIR


def _get_cc_projects_dir() -> Path:
    """Get Claude Code projects directory from env or default."""
    env_dir = os.environ.get("CLAUDE_HOME")
    if env_dir:
        return Path(env_dir) / "projects"
    return DEFAULT_CC_PROJECTS_DIR


def _get_codex_sessions_dir() -> Path:
    """Get Codex CLI sessions directory from env or default."""
    env_dir = os.environ.get("CODEX_SESSIONS_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_CODEX_SESSIONS_DIR


def _get_copilot_state_dir() -> Path:
    """Get Copilot CLI session-state directory from env or default."""
    env_dir = os.environ.get("COPILOT_STATE_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_COPILOT_STATE_DIR


def _session_in_range(session_name: str, start: date, end: date) -> bool:
    """Check if a gptme session directory name falls within a date range.

    gptme session dirs are named like ``YYYY-MM-DD-rest-of-name``.
    """
    try:
        session_date = date.fromisoformat(session_name[:10])
        return start <= session_date <= end
    except (ValueError, IndexError):
        return False


def _quick_date_from_jsonl(jsonl_path: Path) -> date | None:
    """Extract session date from the first timestamped line of a JSONL file.

    Reads only until the first valid timestamp is found (fast).
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


def decode_cc_project_path(encoded: str) -> str:
    """Decode a Claude Code project directory name to the original filesystem path.

    CC encodes workspace paths by replacing ``/`` with ``-``, e.g.::

        -home-bob-bob  ->  /home/bob/bob

    .. warning::
        This decoding is **lossy** for paths whose directory components contain
        hyphens.  Because CC uses ``-`` for both path separators *and* literal
        hyphens in directory names, ``-home-bob-my-project`` is ambiguous: it
        could represent ``/home/bob/my-project`` or ``/home/bob/my/project``
        (among other combinations).  No information is available at decode time
        to distinguish the two cases, so the result may be incorrect for such
        paths.
    """
    if not encoded.startswith("-"):
        return encoded
    return encoded.replace("-", "/")


def parse_gptme_config(session_dir: Path) -> dict:
    """Parse a gptme session's ``config.toml`` for metadata.

    Returns a dict with keys: ``model``, ``workspace``, ``interactive``.
    All keys are always present (empty string / True as defaults).
    """
    config_path = session_dir / "config.toml"
    result = {"model": "", "workspace": "", "interactive": True}
    if not config_path.exists():
        return result
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return result
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        chat = config.get("chat", {})
        result["model"] = chat.get("model", "") or config.get("model", "")
        result["workspace"] = chat.get("workspace", "") or config.get("workspace", "")
        result["interactive"] = chat.get("interactive", config.get("interactive", True))
    except Exception as e:
        logger.debug("Failed to parse %s: %s", config_path, e)
    return result


def discover_gptme_sessions(
    start: date,
    end: date,
    logs_dir: Path | None = None,
) -> list[Path]:
    """Find gptme session directories within a date range.

    Scans ``~/.local/share/gptme/logs/`` (or ``GPTME_LOGS_DIR``) for
    directories whose name starts with an ISO date in ``[start, end]``.

    Returns sorted list of session directory paths.
    """
    if logs_dir is None:
        logs_dir = _get_gptme_logs_dir()
    if not logs_dir.exists():
        logger.debug("gptme logs directory does not exist: %s", logs_dir)
        return []

    sessions: list[Path] = []
    try:
        for entry in sorted(logs_dir.iterdir()):
            if not entry.is_dir():
                continue
            if _session_in_range(entry.name, start, end):
                sessions.append(entry)
    except PermissionError:
        logger.debug("Permission denied reading: %s", logs_dir)
    return sessions


def discover_cc_sessions(
    start: date,
    end: date,
    cc_dir: Path | None = None,
) -> list[Path]:
    """Find Claude Code session JSONL files within a date range.

    Scans ``~/.claude/projects/`` (or ``CLAUDE_HOME/projects/``) for
    session ``.jsonl`` files. Uses quick first-line timestamp extraction
    for fast date filtering.

    Returns sorted list of session JSONL file paths.
    """
    if cc_dir is None:
        cc_dir = _get_cc_projects_dir()
    if not cc_dir.exists():
        logger.debug("CC projects directory does not exist: %s", cc_dir)
        return []

    sessions_with_dates: list[tuple[date, Path]] = []
    try:
        for project_dir in sorted(cc_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                session_date = _quick_date_from_jsonl(jsonl_file)
                if session_date is None:
                    continue
                if start <= session_date <= end:
                    sessions_with_dates.append((session_date, jsonl_file))
    except PermissionError:
        logger.debug("Permission denied reading: %s", cc_dir)
    return [path for _, path in sorted(sessions_with_dates)]


def discover_codex_sessions(
    start: date,
    end: date,
    codex_dir: Path | None = None,
) -> list[Path]:
    """Find Codex CLI session JSONL files within a date range.

    Scans ``~/.codex/sessions/YYYY/MM/DD/`` for rollout JSONL files.
    Uses the directory date structure for fast filtering (no file reads needed).

    Returns sorted list of session JSONL file paths.
    """
    if codex_dir is None:
        codex_dir = _get_codex_sessions_dir()
    if not codex_dir.exists():
        logger.debug("Codex sessions directory does not exist: %s", codex_dir)
        return []

    dated_sessions: list[tuple[date, Path]] = []
    try:
        for year_dir in sorted(codex_dir.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir():
                        continue
                    try:
                        dir_date = date(
                            int(year_dir.name),
                            int(month_dir.name),
                            int(day_dir.name),
                        )
                    except (ValueError, TypeError):
                        continue
                    if not (start <= dir_date <= end):
                        continue
                    for jsonl_file in sorted(day_dir.glob("*.jsonl")):
                        dated_sessions.append((dir_date, jsonl_file))
    except PermissionError:
        logger.debug("Permission denied reading: %s", codex_dir)
    return [path for _, path in sorted(dated_sessions)]


def discover_copilot_sessions(
    start: date,
    end: date,
    copilot_dir: Path | None = None,
) -> list[Path]:
    """Find Copilot CLI session event files within a date range.

    Scans ``~/.copilot/session-state/<uuid>/events.jsonl`` for session files.
    Uses quick first-line timestamp extraction for date filtering.

    Returns sorted list of session JSONL file paths.
    """
    if copilot_dir is None:
        copilot_dir = _get_copilot_state_dir()
    if not copilot_dir.exists():
        logger.debug("Copilot session-state directory does not exist: %s", copilot_dir)
        return []

    sessions_with_dates: list[tuple[date, Path]] = []
    try:
        for session_dir in copilot_dir.iterdir():
            if not session_dir.is_dir():
                continue
            events_file = session_dir / "events.jsonl"
            if not events_file.exists():
                continue
            session_date = _quick_date_from_jsonl(events_file)
            if session_date is None:
                continue
            if start <= session_date <= end:
                sessions_with_dates.append((session_date, events_file))
    except PermissionError:
        logger.debug("Permission denied reading: %s", copilot_dir)
    return [path for _, path in sorted(sessions_with_dates)]


def _quick_first_last_ts(jsonl_path: Path) -> tuple[str | None, str | None]:
    """Extract first and last timestamps from a JSONL file.

    Reads first line for start, seeks to end for last timestamp.
    Returns (first_ts, last_ts) as ISO strings, or None if not found.
    """
    first_ts: str | None = None
    last_ts: str | None = None
    try:
        with open(jsonl_path) as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = entry.get("timestamp")
            if ts_str:
                if first_ts is None:
                    first_ts = ts_str
                last_ts = ts_str
    except (OSError, PermissionError):
        pass
    return first_ts, last_ts


def _duration_from_timestamps(first_ts: str | None, last_ts: str | None) -> int:
    """Compute duration in seconds between two ISO timestamp strings."""
    if not first_ts or not last_ts:
        return 0
    try:
        t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return max(0, int((t2 - t1).total_seconds()))
    except (ValueError, TypeError):
        return 0


def _gptme_session_to_record(session_dir: Path) -> SessionRecord:
    """Convert a discovered gptme session directory to a SessionRecord."""
    from .record import SessionRecord

    config = parse_gptme_config(session_dir)
    # Extract date from directory name (YYYY-MM-DD-rest)
    try:
        session_date = date.fromisoformat(session_dir.name[:10])
        timestamp = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            tzinfo=timezone.utc,
        ).isoformat()
    except (ValueError, IndexError):
        timestamp = ""

    # Try to get duration from conversation.jsonl
    duration = 0
    conv_jsonl = session_dir / "conversation.jsonl"
    if conv_jsonl.exists():
        first_ts, last_ts = _quick_first_last_ts(conv_jsonl)
        duration = _duration_from_timestamps(first_ts, last_ts)
        if first_ts:
            timestamp = (
                first_ts.replace("Z", "+00:00")
                if "+" not in first_ts and first_ts.endswith("Z")
                else first_ts
            )

    run_type = "interactive" if config["interactive"] else "autonomous"

    return SessionRecord(
        session_id=session_dir.name[11:] if len(session_dir.name) > 11 else session_dir.name,
        timestamp=timestamp,
        harness="gptme",
        model=config["model"] or None,
        run_type=run_type,
        duration_seconds=duration,
    )


def _cc_session_to_record(jsonl_path: Path) -> SessionRecord:
    """Convert a discovered Claude Code session JSONL to a SessionRecord."""
    from .record import SessionRecord

    first_ts, last_ts = _quick_first_last_ts(jsonl_path)
    duration = _duration_from_timestamps(first_ts, last_ts)
    timestamp = ""
    if first_ts:
        timestamp = (
            first_ts.replace("Z", "+00:00")
            if "+" not in first_ts and first_ts.endswith("Z")
            else first_ts
        )

    # CC project dir name encodes the workspace path
    project_name = jsonl_path.parent.name

    return SessionRecord(
        session_id=jsonl_path.stem,
        timestamp=timestamp,
        harness="claude-code",
        model=None,  # CC doesn't store model in JSONL reliably
        run_type="unknown",
        duration_seconds=duration,
        journal_path=decode_cc_project_path(project_name),
    )


def _codex_session_to_record(jsonl_path: Path) -> SessionRecord:
    """Convert a discovered Codex CLI session JSONL to a SessionRecord."""
    from .record import SessionRecord

    # Date from directory structure: YYYY/MM/DD
    try:
        day_dir = jsonl_path.parent
        month_dir = day_dir.parent
        year_dir = month_dir.parent
        session_date = date(int(year_dir.name), int(month_dir.name), int(day_dir.name))
        timestamp = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            tzinfo=timezone.utc,
        ).isoformat()
    except (ValueError, TypeError):
        timestamp = ""

    return SessionRecord(
        session_id=jsonl_path.stem,
        timestamp=timestamp,
        harness="codex",
        duration_seconds=0,
    )


def _copilot_session_to_record(events_path: Path) -> SessionRecord:
    """Convert a discovered Copilot CLI events.jsonl to a SessionRecord."""
    from .record import SessionRecord

    first_ts, last_ts = _quick_first_last_ts(events_path)
    duration = _duration_from_timestamps(first_ts, last_ts)
    timestamp = ""
    if first_ts:
        timestamp = (
            first_ts.replace("Z", "+00:00")
            if "+" not in first_ts and first_ts.endswith("Z")
            else first_ts
        )

    return SessionRecord(
        session_id=events_path.parent.name,
        timestamp=timestamp,
        harness="copilot",
        duration_seconds=duration,
    )


def discover_all(
    since_days: int = 30,
    gptme_logs_dir: Path | None = None,
    cc_dir: Path | None = None,
    codex_dir: Path | None = None,
    copilot_dir: Path | None = None,
) -> list[SessionRecord]:
    """Discover sessions across all harnesses and return as SessionRecords.

    This is the main entry point for discovery-based session listing.
    Scans gptme, Claude Code, Codex, and Copilot session directories
    for the given time window and converts them to SessionRecord objects.

    Records are sorted chronologically by timestamp.
    """
    end = date.today()
    start = end - timedelta(days=since_days)

    records: list[SessionRecord] = []

    for path in discover_gptme_sessions(start, end, logs_dir=gptme_logs_dir):
        records.append(_gptme_session_to_record(path))

    for path in discover_cc_sessions(start, end, cc_dir=cc_dir):
        records.append(_cc_session_to_record(path))

    for path in discover_codex_sessions(start, end, codex_dir=codex_dir):
        records.append(_codex_session_to_record(path))

    for path in discover_copilot_sessions(start, end, copilot_dir=copilot_dir):
        records.append(_copilot_session_to_record(path))

    # Sort by timestamp
    records.sort(key=lambda r: r.timestamp or "")
    return records
