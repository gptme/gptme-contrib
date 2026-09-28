"""Tests for the journal skill's no-overwrite entry helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "journal"
    / "scripts"
    / "journal-new.sh"
)


def run_helper(
    root: Path,
    name: str = "session",
    *,
    body: str = "# Notes\n",
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "JOURNAL_ROOT": str(root), **environment}
    return subprocess.run(
        ["bash", str(SCRIPT), name],
        input=body,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_started_timestamp_selects_work_day(tmp_path: Path) -> None:
    result = run_helper(
        tmp_path,
        "late-write",
        JOURNAL_STARTED="2026-09-26T23:55:00+02:00",
    )

    target = tmp_path / "2026-09-26" / "late-write.md"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)
    assert target.read_text() == "# Notes\n"


def test_explicit_work_date_creates_backdated_entry(tmp_path: Path) -> None:
    result = run_helper(tmp_path, "backfill.md", JOURNAL_DATE="2026-09-24")

    target = tmp_path / "2026-09-24" / "backfill.md"
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "# Notes\n"


def test_existing_entry_is_never_overwritten(tmp_path: Path) -> None:
    first = run_helper(tmp_path, JOURNAL_DATE="2026-09-24", body="first\n")
    second = run_helper(tmp_path, JOURNAL_DATE="2026-09-24", body="second\n")

    target = tmp_path / "2026-09-24" / "session.md"
    assert first.returncode == 0
    assert second.returncode == 1
    assert "refusing to overwrite" in second.stderr
    assert target.read_text() == "first\n"


def test_conflicting_started_and_explicit_date_fail(tmp_path: Path) -> None:
    result = run_helper(
        tmp_path,
        JOURNAL_DATE="2026-09-27",
        JOURNAL_STARTED="2026-09-26T23:55:00+02:00",
    )

    assert result.returncode == 2
    assert "disagrees" in result.stderr
    assert not list(tmp_path.rglob("*.md"))


def test_invalid_date_and_path_name_fail(tmp_path: Path) -> None:
    invalid_date = run_helper(tmp_path, JOURNAL_DATE="2026-02-30")
    nested_name = run_helper(tmp_path, "../escape", JOURNAL_DATE="2026-09-24")

    assert invalid_date.returncode == 2
    assert "invalid JOURNAL_DATE" in invalid_date.stderr
    assert nested_name.returncode == 2
    assert "single non-empty filename" in nested_name.stderr
    assert not list(tmp_path.rglob("*.md"))
