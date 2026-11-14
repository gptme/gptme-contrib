"""Canonical shell subprocess helper.

Replaces ~4 per-script ``run_cmd`` definitions in generate-backlog-ideas.py,
ideation.py, weekly-review.py, and quarterly-metrics.py that were byte-identical
except for the default timeout and an optional cwd override.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

__all__ = ["gh_json", "gh_paginated_list", "run_cmd"]


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


def gh_json(args: list[str], timeout: int = 60) -> object | None:
    """Run ``gh <args>`` and return parsed JSON, or None on failure.

    Args:
        args: Arguments for the ``gh`` CLI (e.g. ``["pr", "view", "1"]``).
        timeout: Seconds before the call is abandoned. Defaults to 60.

    Returns:
        Parsed JSON, or None on timeout, missing ``gh``, non-zero exit, empty
        output, or a decode error.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed: object = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return parsed


def gh_paginated_list(args: list[str], timeout: int = 60) -> list[dict]:
    """Return all ``gh api`` pages as one flat list of dicts.

    ``gh api --paginate`` prints one JSON document per page. ``--slurp`` wraps
    pages in an outer list; this flattens that and also accepts already-flat
    responses from older ``gh`` shims. Non-dict items are dropped.

    Args:
        args: Arguments for ``gh api`` (without ``--paginate``/``--slurp``).
        timeout: Seconds per call. Defaults to 60.

    Returns:
        Flat list of dicts from all pages; empty on any failure.
    """
    parsed = gh_json([*args, "--paginate", "--slurp"], timeout=timeout)
    if not isinstance(parsed, list):
        return []
    if all(isinstance(page, list) for page in parsed):
        items: list[object] = [item for page in parsed for item in page]
    else:
        items = list(parsed)
    return [item for item in items if isinstance(item, dict)]
