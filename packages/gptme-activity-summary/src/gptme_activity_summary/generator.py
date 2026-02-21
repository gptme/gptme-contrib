"""
Journal summarization generator.

This module handles:
1. Finding journal entries for dates
2. Saving summaries to disk
3. Utility functions for summary generation

Note: The actual summarization is done by the Claude Code backend (cc_backend.py).
"""

import os
import subprocess
from datetime import date
from pathlib import Path

from .schemas import (
    DailySummary,
    MonthlySummary,
    WeeklySummary,
)


def _detect_workspace() -> Path:
    """Detect agent workspace directory.

    Priority:
    1. GPTME_WORKSPACE environment variable
    2. gptme.dirs.get_workspace() (if available)
    3. Git repository root (fallback)
    """
    # 1. Explicit env var
    ws = os.environ.get("GPTME_WORKSPACE")
    if ws:
        return Path(ws)

    # 2. Try gptme's get_workspace
    try:
        from gptme.dirs import get_workspace

        return get_workspace()
    except (ImportError, AttributeError):
        pass

    # 3. Fall back to git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


# Derive paths from workspace
WORKSPACE = _detect_workspace()
JOURNAL_DIR = WORKSPACE / "journal"
SUMMARIES_DIR = WORKSPACE / "knowledge" / "summaries"


def get_journal_entries_for_date(target_date: date) -> list[Path]:
    """Get all journal entry files for a specific date.

    Supports both formats:
    - Old: journal/YYYY-MM-DD-session123.md (single file per day)
    - New: journal/YYYY-MM-DD/*.md (directory with multiple files per day)

    Args:
        target_date: The date to find entries for

    Returns:
        Sorted list of Path objects to journal entry files
    """
    date_prefix = target_date.isoformat()
    entries: list[Path] = []

    # Old format: files in journal root with date prefix
    entries.extend(JOURNAL_DIR.glob(f"{date_prefix}*.md"))

    # New format: directory with date as name
    date_dir = JOURNAL_DIR / date_prefix
    if date_dir.is_dir():
        entries.extend(date_dir.glob("*.md"))

    return sorted(entries)


def save_summary(summary: DailySummary | WeeklySummary | MonthlySummary) -> Path:
    """Save a summary to disk.

    Args:
        summary: DailySummary, WeeklySummary, or MonthlySummary object

    Returns:
        Path to the saved file
    """
    if isinstance(summary, DailySummary):
        output_dir = SUMMARIES_DIR / "daily"
        filename = f"{summary.date.isoformat()}.md"
    elif isinstance(summary, WeeklySummary):
        output_dir = SUMMARIES_DIR / "weekly"
        filename = f"{summary.week}.md"
    elif isinstance(summary, MonthlySummary):
        output_dir = SUMMARIES_DIR / "monthly"
        filename = f"{summary.month}.md"
    else:
        raise ValueError(f"Unknown summary type: {type(summary)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path: Path = output_dir / filename

    # Write markdown
    output_path.write_text(summary.to_markdown())

    # Also write JSON for programmatic access
    import json
    from dataclasses import asdict

    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(asdict(summary), default=str, indent=2))

    return output_path
