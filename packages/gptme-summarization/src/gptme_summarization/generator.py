"""
Journal summarization generator.

This module handles:
1. Finding journal entries for dates
2. Saving summaries to disk
3. Utility functions for summary generation

Note: The actual summarization is done by the Claude Code backend (cc_backend.py).
"""

import subprocess
from datetime import date
from pathlib import Path

from .schemas import (
    DailySummary,
    WeeklySummary,
    MonthlySummary,
)


def _get_agent_workspace() -> Path:
    """Get the agent workspace directory.

    Detection order:
    1. GPTME_WORKSPACE environment variable (if explicitly set)
    2. Parent git repository root (if in a submodule like gptme-contrib)
    3. Current git repository root (if in a main repo)
    4. Current working directory (fallback)

    This handles the case where code runs from within a submodule
    (e.g., gptme-contrib) and needs to find the parent agent workspace.

    Returns:
        Path to the agent workspace directory
    """
    import os

    # 1. Check for explicit environment variable
    if workspace := os.environ.get("GPTME_WORKSPACE"):
        return Path(workspace)

    # 2. Try to find git root, handling submodules
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_root = Path(result.stdout.strip())

            # Check if we're in a submodule (.git is a file, not a directory)
            git_path = git_root / ".git"
            if git_path.is_file():
                # We're in a submodule, the parent directory is the agent workspace
                parent = git_root.parent
                # Verify the parent has a .git directory (is a git repo)
                if (parent / ".git").exists():
                    return parent
                # If parent isn't a git repo, return the submodule root
                return git_root

            # Not a submodule, return the git root
            return git_root
    except Exception:
        pass

    # 3. Fall back to current directory
    return Path.cwd()


# Derive paths from workspace
WORKSPACE = _get_agent_workspace()
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
    import json
    from dataclasses import asdict

    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(asdict(summary), default=str, indent=2))

    return output_path
