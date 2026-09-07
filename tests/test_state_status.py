"""Regression tests for scripts/state-status.py."""

import subprocess
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "state-status.py"


def test_running_outside_git_repo_reports_an_error(tmp_path: Path) -> None:
    """The CLI must not silently treat an arbitrary directory as a repo root."""
    result = subprocess.run(
        [str(SCRIPT_PATH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"RuntimeError: No git repository found above {tmp_path}" in result.stderr
