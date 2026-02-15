"""
Journal summarization generator.

This module handles:
1. Finding journal entries for dates
2. Saving summaries to disk
3. Utility functions for summary generation

Note: The actual summarization is done by the Claude Code backend (cc_backend.py).
"""

from datetime import date
from pathlib import Path

from .schemas import (
    DailySummary,
    WeeklySummary,
    MonthlySummary,
)

# Journal directory (relative to workspace root)
JOURNAL_DIR = Path("/home/bob/bob/journal")
SUMMARIES_DIR = Path("/home/bob/bob/knowledge/summaries")


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


def save_summary(summary) -> Path:
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
    output_path = output_dir / filename

    # Write markdown
    output_path.write_text(summary.to_markdown())

    # Also write JSON for programmatic access
    json_path = output_path.with_suffix(".json")
    json_path.write_text(summary.to_json())

    return output_path
