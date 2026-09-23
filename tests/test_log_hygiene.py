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


def _vacuum_with_fake_journalctl(
    tmp_path: Path, vacuum_exit: int, vacuum_stderr: str
) -> str:
    """Run hygiene_vacuum_user_journal with a stubbed journalctl on PATH; return stdout.

    The stub answers --disk-usage with a fixed size and makes the vacuum calls
    behave per the args, so we can exercise the permission-denied branch without
    a real (root-owned) journal.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "journalctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [[ "$a" == "--disk-usage" ]]; then\n'
        '    echo "Archived and active journals take up 1.2G in the file system."\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'>&2 printf "%s" "{vacuum_stderr}"\n'
        f"exit {vacuum_exit}\n"
    )
    fake.chmod(0o755)
    script = f'source "{LIB}"; hygiene_vacuum_user_journal 200M 30d false'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    return proc.stdout


def test_journal_permission_denied_warns_not_silent(tmp_path: Path) -> None:
    # A root-owned system journal (Storage=persistent) rejects a non-root vacuum.
    # The function must WARN, never pretend it capped anything.
    out = _vacuum_with_fake_journalctl(
        tmp_path, vacuum_exit=1, vacuum_stderr="Failed to ...: Permission denied\n"
    )
    assert "WARN" in out
    assert "not user-manageable" in out


def test_journal_clean_vacuum_does_not_warn(tmp_path: Path) -> None:
    # When the user genuinely owns the journal, the vacuum succeeds silently and
    # the honesty WARN must NOT fire (no false alarm on healthy forks).
    out = _vacuum_with_fake_journalctl(tmp_path, vacuum_exit=0, vacuum_stderr="")
    assert "WARN" not in out
    assert "User journal after" in out


def _prune_uv_cache(tmp_path: Path, dry_run: str = "false") -> tuple[str, str]:
    """Run hygiene_prune_uv_cache with a stubbed uv binary; return (stdout, stderr)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cachedir = tmp_path / "uv-cache"
    cachedir.mkdir()
    # Stub that reports a fake cache dir and records which subcommand was called.
    call_log = tmp_path / "uv-calls.log"
    fake_uv = bindir / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{call_log}"\n'
        # For `uv cache dir` → echo the fake cache dir
        'if [[ "$1 $2" == "cache dir" ]]; then\n'
        f'  echo "{cachedir}"\n'
        "  exit 0\n"
        "fi\n"
        # For `uv cache prune --force` → pretend to prune
        'if [[ "$1 $2 $3" == "cache prune --force" ]]; then\n'
        '  echo "Pruned 12 packages"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_uv.chmod(0o755)
    script = f'source "{LIB}"; hygiene_prune_uv_cache {dry_run}'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return proc.stdout, proc.stderr


def test_uv_prune_calls_force_flag(tmp_path: Path) -> None:
    # Must use `uv cache prune --force`, never `uv cache clean` or bare `prune`.
    _, _ = _prune_uv_cache(tmp_path, dry_run="false")
    call_log = tmp_path / "uv-calls.log"
    calls = call_log.read_text().splitlines()
    assert any("cache prune --force" in c for c in calls), f"unexpected calls: {calls}"
    assert not any("cache clean" in c for c in calls), "must not call uv cache clean"


def test_uv_prune_dry_run_skips_prune(tmp_path: Path) -> None:
    out, _ = _prune_uv_cache(tmp_path, dry_run="true")
    call_log = tmp_path / "uv-calls.log"
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    assert "dry-run" in out
    assert not any("prune --force" in c for c in calls), "dry-run must not prune"


def test_uv_prune_degrades_when_absent(tmp_path: Path) -> None:
    # When uv is not in PATH, the function degrades visibly (no error, skip message).
    script = f'source "{LIB}"; hygiene_prune_uv_cache false'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert "not present" in proc.stdout
    assert "degrade" in proc.stdout
