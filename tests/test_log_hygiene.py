"""Tests for scripts/lib/log-hygiene.sh.

Reusable disk/log-hygiene mechanism. It only ever touches ephemeral operational
state — gate results, per-session temp caches, and OS journals — never
trajectories or session records. A regression that either (a) deleted too much
or (b) silently stopped pruning would be invisible until a months-long VM filled
its disk, so pin the two properties that matter: correct age cutoff, and dry-run
touches nothing.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib" / "log-hygiene.sh"


def _prune(tmp_path: Path, glob: str, keep_days: int, dry_run: str) -> tuple[int, str]:
    """Source the lib and call hygiene_prune_old_files; return (deleted_count, stderr)."""
    script = f'source "{LIB}"; hygiene_prune_old_files "{tmp_path}" "{glob}" {keep_days} {dry_run}'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    # The count is echoed on the last stdout line.
    count = int(proc.stdout.strip().splitlines()[-1])
    return count, proc.stderr


def _aged_file(tmp_path: Path, name: str, age_days: float) -> Path:
    f = tmp_path / name
    f.write_text("x")
    old = time.time() - age_days * 86400
    import os

    os.utime(f, (old, old))
    return f


def test_prunes_only_files_older_than_cutoff(tmp_path: Path) -> None:
    old = _aged_file(tmp_path, "old.state", age_days=20)
    fresh = _aged_file(tmp_path, "fresh.state", age_days=1)

    count, _ = _prune(tmp_path, "*.state", keep_days=14, dry_run="false")

    assert count == 1
    assert not old.exists()
    assert fresh.exists()


def test_glob_scopes_deletion(tmp_path: Path) -> None:
    old_state = _aged_file(tmp_path, "old.state", age_days=20)
    old_json = _aged_file(tmp_path, "old.json", age_days=20)

    count, _ = _prune(tmp_path, "*.state", keep_days=14, dry_run="false")

    assert count == 1
    assert not old_state.exists()
    assert old_json.exists()  # different glob — untouched


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    old = _aged_file(tmp_path, "old.state", age_days=99)

    count, stderr = _prune(tmp_path, "*.state", keep_days=14, dry_run="true")

    assert count == 1  # reports what it *would* delete
    assert old.exists()  # but leaves it in place
    assert "Would delete" in stderr


def test_missing_dir_is_safe(tmp_path: Path) -> None:
    count, _ = _prune(
        tmp_path / "does-not-exist", "*.state", keep_days=14, dry_run="false"
    )
    assert count == 0
