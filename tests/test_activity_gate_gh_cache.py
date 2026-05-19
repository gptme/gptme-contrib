"""Tests for per-repo gh list caching in activity-gate.sh.

Regression for PR #934 review feedback: failed `gh` producers must not cache the
fallback `[]`, or the gate will suppress activity for the full TTL.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

TEST_REPO = "testorg/testrepo"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import os, sys
from pathlib import Path

argv = sys.argv[1:]
count_file = Path(os.environ["GH_PR_LIST_CALL_COUNT"])
fail_pr_list = os.environ.get("TEST_FAIL_PR_LIST") == "1"

def bump() -> None:
    n = int(count_file.read_text().strip()) if count_file.exists() else 0
    count_file.write_text(str(n + 1))

if not argv:
    sys.exit(2)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    bump()
    if fail_pr_list:
        sys.exit(1)
    print("[]")
    sys.exit(0)

if argv[0] == "issue" and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "run" and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] == "api":
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    *,
    fail_pr_list: bool,
) -> tuple[subprocess.CompletedProcess[str], int]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    count_file = tmp / "pr-list-count.txt"

    env = os.environ.copy()
    env["GH_PR_LIST_CALL_COUNT"] = str(count_file)
    env["TEST_FAIL_PR_LIST"] = "1" if fail_pr_list else "0"
    env["PATH"] = f"{tmp}:{env['PATH']}"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--org",
            "testorg",
            "--repo",
            TEST_REPO,
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    count = int(count_file.read_text().strip()) if count_file.exists() else 0
    return result, count


def test_failed_pr_fetch_does_not_seed_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        cache_file = state_dir / "gh-cache" / "pr-testorg-testrepo.json"

        first, first_count = _run_gate(tmp, state_dir, fail_pr_list=True)
        assert first.returncode in (0, 1), first.stderr
        assert first_count == 1
        assert not cache_file.exists(), "failed gh pr list must not populate cache"

        second, second_count = _run_gate(tmp, state_dir, fail_pr_list=False)
        assert second.returncode in (0, 1), second.stderr
        assert second_count == 2, "second run should re-fetch after prior failure"
        assert cache_file.exists(), "successful gh pr list should populate cache"
