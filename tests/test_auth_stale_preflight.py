"""Contract tests for scripts/auth-stale-preflight.sh.

The preflight is the gate every dispatcher runs before launching a Claude Code
session. Its exit contract (0 proceed / 75 skip) plus the "TTL is a re-probe
cadence, not a recovery signal" logic is what stops a persistent logout from
turning into one burned session every 30 minutes forever, and what stops a
no-marker logout from burning ~80s per doomed launch.

Because the live probe shells out to ``claude auth status --json``, these tests
inject a fake ``claude`` on PATH whose behaviour (logged-in / logged-out /
absent) is the axis under test. Slot resolution is pinned via single-slot mode.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

PREFLIGHT = Path(__file__).resolve().parents[1] / "scripts" / "auth-stale-preflight.sh"

_FAKE_LOGGED_IN = "#!/usr/bin/env bash\necho '{\"loggedIn\": true}'\n"
_FAKE_LOGGED_OUT = "#!/usr/bin/env bash\necho '{\"loggedIn\": false}'\n"


def _bindir_with_claude(tmp_path: Path, body: str | None) -> Path:
    """Create a bin dir; optionally place a fake ``claude`` in it.

    ``body=None`` → no claude on PATH → probe reports unavailable (rc 2).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if body is not None:
        claude = bindir / "claude"
        claude.write_text(body)
        claude.chmod(0o755)
    return bindir


def _run(
    stale_dir: Path,
    *,
    claude_body: str | None,
    slot: str = "test",
    backend: str = "claude-code",
) -> subprocess.CompletedProcess[str]:
    bindir = _bindir_with_claude(stale_dir.parent, claude_body)
    env = {
        "HOME": str(stale_dir.parent),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "STALE_DIR": str(stale_dir),
        "AUTH_SINGLE_SLOT": "1",
        "AUTH_SLOT_NAME": slot,
    }
    return subprocess.run(
        ["bash", str(PREFLIGHT), backend],
        capture_output=True,
        text=True,
        env=env,
    )


def _marker(stale_dir: Path, slot: str = "test") -> Path:
    return stale_dir / f"claude-code-{slot}-auth-stale.txt"


def test_no_marker_probe_ok_proceeds(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    assert _run(stale, claude_body=_FAKE_LOGGED_IN).returncode == 0


def test_no_marker_probe_unavailable_proceeds(tmp_path: Path) -> None:
    # No claude on PATH → probe unavailable → fail open (historical behaviour).
    stale = tmp_path / "state"
    stale.mkdir()
    assert _run(stale, claude_body=None).returncode == 0


def test_no_marker_probe_logged_out_blocks(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    assert _run(stale, claude_body=_FAKE_LOGGED_OUT).returncode == 75


def test_fresh_marker_blocks(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    _marker(stale).write_text("fresh\n")
    # Even with a passing probe, a fresh marker blocks (cheap block path).
    assert _run(stale, claude_body=_FAKE_LOGGED_IN).returncode == 75


def test_expired_marker_probe_ok_clears_and_proceeds(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    m = _marker(stale)
    m.write_text("old\n")
    old = time.time() - 4000  # > MAX_AGE_SEC default (1800)
    os.utime(m, (old, old))
    res = _run(stale, claude_body=_FAKE_LOGGED_IN)
    assert res.returncode == 0
    assert not m.exists()  # cleared


def test_expired_marker_probe_logged_out_retains_and_blocks(tmp_path: Path) -> None:
    stale = tmp_path / "state"
    stale.mkdir()
    m = _marker(stale)
    m.write_text("old\n")
    old = time.time() - 4000
    os.utime(m, (old, old))
    res = _run(stale, claude_body=_FAKE_LOGGED_OUT)
    assert res.returncode == 75
    assert m.exists()  # retained for retry, mtime refreshed
