"""Tests for per-repo gh list caching in activity-gate.sh.

Regression for PR #934 review feedback: failed `gh` producers must not cache the
fallback `[]`, or the gate will suppress activity for the full TTL.

Also verifies that merge-conflict detection still bypasses the PR cache so the
"nag every run" contract remains true even within the cache window.
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
        # Failed cached fetch returns the empty fallback, so the live merge-status
        # fetch is skipped (re-calling a failing API would only burn another
        # GraphQL call). One PR-list call total this run.
        assert first_count == 1
        assert not cache_file.exists(), "failed gh pr list must not populate cache"

        second, second_count = _run_gate(tmp, state_dir, fail_pr_list=False)
        assert second.returncode in (0, 1), second.stderr
        # Re-fetch succeeds and seeds the cache; the result is an empty PR list,
        # so the live fetch is still skipped (no open PRs to check). +1 call.
        assert second_count == 2, "second run should re-fetch after prior failure"
        assert cache_file.exists(), "successful gh pr list should populate cache"


FAKE_GH_CONFLICT_REGRESSION = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
count_file = Path(os.environ["GH_PR_LIST_CALL_COUNT"])

def bump() -> int:
    n = int(count_file.read_text().strip()) if count_file.exists() else 0
    n += 1
    count_file.write_text(str(n))
    return n

if not argv:
    sys.exit(2)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    call = bump()
    merge_state = "CLEAN"
    mergeable = "MERGEABLE"
    # First run does two PR fetches (cached + live), both clean. The next run's
    # live merge-status fetch flips to conflict even though the cached PR data
    # is still fresh.
    if call >= 3:
        merge_state = "DIRTY"
        mergeable = "CONFLICTING"
    pr = [{
        "number": 17,
        "title": "Conflicting PR",
        "updatedAt": "2026-05-19T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "headRefOid": "deadbeef",
        "isDraft": False,
    }]
    print(json.dumps(pr))
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
    # No Greptile comments, no notifications.
    sys.exit(0)

sys.exit(0)
"""


def _run_gate_jsonl(
    tmp: Path,
    state_dir: Path,
    fake_gh_source: str,
) -> tuple[subprocess.CompletedProcess[str], int]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(fake_gh_source)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    count_file = tmp / "pr-list-count.txt"

    env = os.environ.copy()
    env["GH_PR_LIST_CALL_COUNT"] = str(count_file)
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
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    count = int(count_file.read_text().strip()) if count_file.exists() else 0
    return result, count


def test_merge_conflict_check_bypasses_pr_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        first, first_count = _run_gate_jsonl(
            tmp, state_dir, FAKE_GH_CONFLICT_REGRESSION
        )
        assert first.returncode in (0, 1), first.stderr
        assert first_count == 2, "first run should do cached + live PR fetches"

        second, second_count = _run_gate_jsonl(
            tmp, state_dir, FAKE_GH_CONFLICT_REGRESSION
        )
        assert second.returncode in (0, 1), second.stderr
        assert (
            second_count == 3
        ), "second run should skip cached producer but still do one live PR fetch"
        assert '"type":"merge_conflict"' in second.stdout, second.stdout


# Empty-repo stub: no open author PRs. The cached PR-list fetch returns [],
# so the live merge-status fetch must be skipped (it could only return [] too).
FAKE_GH_EMPTY_PRS = r"""#!/usr/bin/env python3
from __future__ import annotations
import os, sys
from pathlib import Path

argv = sys.argv[1:]
count_file = Path(os.environ["GH_PR_LIST_CALL_COUNT"])

def bump() -> int:
    n = int(count_file.read_text().strip()) if count_file.exists() else 0
    n += 1
    count_file.write_text(str(n))
    return n

if not argv:
    sys.exit(2)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    bump()
    print("[]")
    sys.exit(0)

if argv[0] in ("issue", "run") and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

# repo list / api / anything else: succeed silently.
sys.exit(0)
"""


def test_empty_repo_skips_live_pr_fetch() -> None:
    """A repo with no open author PRs should do exactly one PR-list fetch
    per run (the cached lane). The live merge-status fetch is skipped because
    it could only re-fetch the same empty set — this removes the dominant
    idle-time GraphQL burner. See github-graphql-rate-limit-regression."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        first, first_count = _run_gate_jsonl(tmp, state_dir, FAKE_GH_EMPTY_PRS)
        assert first.returncode in (0, 1), first.stderr
        # Only the cached fetch fires; live fetch is skipped (was 2 before).
        assert first_count == 1, "empty repo should do one PR fetch, not two"

        # Second run: cached [] is still fresh, live still skipped -> no calls.
        second, second_count = _run_gate_jsonl(tmp, state_dir, FAKE_GH_EMPTY_PRS)
        assert second.returncode in (0, 1), second.stderr
        assert second_count == 1, "second run should add no PR fetches for empty repo"
        assert "merge_conflict" not in second.stdout, second.stdout
