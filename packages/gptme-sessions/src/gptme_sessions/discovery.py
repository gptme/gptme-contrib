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
from datetime import date, datetime
from pathlib import Path

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
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        return ts.date()
                    except (ValueError, TypeError):
                        continue
    except (OSError, UnicodeDecodeError) as e:
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


def extract_cc_model(jsonl_path: Path) -> str | None:
    """Extract the model from the first assistant message in a Claude Code JSONL file.

    CC JSONL lines have the structure::

        {"message": {"role": "assistant", "model": "claude-opus-4-6", ...}, ...}

    Scans up to 50 lines to find an assistant message with a model field.
    """
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("message", {})
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    model = msg.get("model")
                    if model:
                        return str(model)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to read %s for model extraction: %s", jsonl_path, e)
    return None


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
