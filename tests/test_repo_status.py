"""Regression tests for scripts/github/repo-status.sh."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "repo-status.sh"

FAKE_GH = r"""#!/usr/bin/env python3
import json
import sys

argv = sys.argv[1:]

if argv[:2] == ["api", "user"]:
    print("TimeToBuildBob")
    raise SystemExit(0)

if len(argv) >= 2 and argv[0] == "api" and argv[1].startswith("repos/"):
    if argv[1].endswith("/commits"):
        print("def9999")
    else:
        print("master")
    raise SystemExit(0)

if argv[:2] == ["run", "list"]:
    print(
        json.dumps(
            [
                {
                    "databaseId": 30174924588,
                    "conclusion": "failure",
                    "status": "completed",
                    "url": "https://example.test/run/30174924588",
                    "name": "Tests",
                    "headSha": "abc1234",
                }
            ]
        )
    )
    raise SystemExit(0)

if argv[:2] == ["workflow", "list"]:
    print("[]")
    raise SystemExit(0)

raise SystemExit(f"unexpected gh invocation: {argv}")
"""


def test_stale_suffix_uses_workflow_run_id() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        env = os.environ.copy()
        env.pop("GH_FORCE_TTY", None)
        env.pop("NO_COLOR", None)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["GH_USER"] = "TimeToBuildBob"

        result = subprocess.run(
            ["bash", str(SCRIPT), "owner/repo:FocusedRepo"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert (
        "FocusedRepo: Failing (stale; HEAD=def9999, run=30174924588)" in result.stdout
    )
    assert "run=abc1234" not in result.stdout
