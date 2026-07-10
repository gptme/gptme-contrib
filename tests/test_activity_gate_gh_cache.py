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
        # Both the cached and live PR fetches fire. The cached fetch fails (no
        # cache written), so the live fetch also misses the cache and fails too.
        # Two PR-list calls total, neither populates the cache.
        assert first_count == 2
        assert not cache_file.exists(), "failed gh pr list must not populate cache"

        second, second_count = _run_gate(tmp, state_dir, fail_pr_list=False)
        assert second.returncode in (0, 1), second.stderr
        # Re-fetch succeeds: cached write populates the cache; live lane uses the
        # same freshly-written cache (age ~0 < GH_CACHE_TTL_LIVE_PR). One new
        # call in run 2, cumulative total = 3 (2 from run 1 + 1 here).
        assert second_count == 3, "second run should re-fetch after prior failure"
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
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(fake_gh_source)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    count_file = tmp / "pr-list-count.txt"

    env = os.environ.copy()
    env["GH_PR_LIST_CALL_COUNT"] = str(count_file)
    env["PATH"] = f"{tmp}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)

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
    # Set GH_CACHE_TTL_LIVE_PR=0 so the live lane always fetches fresh data
    # regardless of the shared pr-data cache age. This tests the invariant
    # that merge-conflict detection is not suppressed within the 480s pr-data
    # cache window: a PR that becomes DIRTY between runs must be flagged on the
    # very next run, not after the cache expires.
    live_uncached = {"GH_CACHE_TTL_LIVE_PR": "0"}

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        first, first_count = _run_gate_jsonl(
            tmp, state_dir, FAKE_GH_CONFLICT_REGRESSION, extra_env=live_uncached
        )
        assert first.returncode in (0, 1), first.stderr
        assert first_count == 2, "first run should do cached + live PR fetches"

        second, second_count = _run_gate_jsonl(
            tmp, state_dir, FAKE_GH_CONFLICT_REGRESSION, extra_env=live_uncached
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


def test_empty_repo_live_pr_fetch_uses_shared_cache() -> None:
    """A repo with no open author PRs should produce exactly one actual GraphQL
    round-trip per run. The live fetch (fetch_live_pr_data) IS called — the gate
    that previously skipped it for empty repos was removed — but both the cached
    and live lanes share the same cache key, so the live hit is free from the
    cache written by the cached lane moments before. Net: 1 PR-list call per run.
    See github-graphql-rate-limit-regression and contrib#1259."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        first, first_count = _run_gate_jsonl(tmp, state_dir, FAKE_GH_EMPTY_PRS)
        assert first.returncode in (0, 1), first.stderr
        # Cached fetch fires (cache miss), live fetch is served from the
        # just-written cache (age ~0 < GH_CACHE_TTL_LIVE_PR). 1 call total.
        assert first_count == 1, "empty repo should do one actual PR fetch, not two"

        # Second run: both lanes hit fresh cache -> no calls.
        second, second_count = _run_gate_jsonl(tmp, state_dir, FAKE_GH_EMPTY_PRS)
        assert second.returncode in (0, 1), second.stderr
        assert (
            second_count == 1
        ), "second run should add no actual PR fetches for empty repo"
        assert "merge_conflict" not in second.stdout, second.stdout
