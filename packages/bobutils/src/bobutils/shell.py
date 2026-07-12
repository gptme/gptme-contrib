"""Canonical shell subprocess helper.

Replaces ~4 per-script ``run_cmd`` definitions in generate-backlog-ideas.py,
ideation.py, weekly-review.py, and quarterly-metrics.py that were byte-identical
except for the default timeout and an optional cwd override.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["run_cmd"]


def run_cmd(
    cmd: list[str],
    timeout: int = 30,
    *,
    cwd: Path | str | None = None,
) -> str:
    """Run a shell command and return stripped stdout, or '' on failure.

    Args:
        cmd: Command and arguments.
        timeout: Seconds before subprocess.TimeoutExpired. Defaults to 30.
        cwd: Working directory. Defaults to the current process directory.

    Returns:
        stdout stripped of leading/trailing whitespace, or '' on timeout or
        missing executable.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=True
        )
        return result.stdout.strip()
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        subprocess.CalledProcessError,
    ):
        return ""
