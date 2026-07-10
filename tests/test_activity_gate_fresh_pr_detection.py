"""Regression test for the fresh-PR detection gap in activity-gate.sh.

Before the fix (contrib#1260): when the 480s pr-data cache showed [], the gate
hardcoded live_pr_data=[] instead of calling fetch_live_pr_data. A PR opened
after the last cache write was invisible to check_merge_ready for up to 480s,
even though the 180s live cache would expire and re-fetch first.

After the fix: fetch_live_pr_data is always called. When its shorter TTL expires
the live lane re-fetches and surfaces the PR, while the 480s pr-data cache
continues to show [].

Concrete incident: gptme-contrib#1259 sat at CLEAN+MERGEABLE+Greptile 5/5 for
~1h without any merge_ready emission. The state dir had no state file for the PR
at all — it was never enumerated by the gate.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

TEST_REPO = "testorg/testrepo"
TEST_PR_NUMBER = 1259
TEST_HEAD_SHA = "abc123def456"

# Fake gh: always returns a single CLEAN+MERGEABLE PR, has merge permission,
# no prior bot comments, no Greptile review (missing state file = OK to merge).
FAKE_GH_FRESH_PR = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
count_file = os.environ.get("GH_PR_LIST_CALL_COUNT", "")

def bump() -> int:
    if not count_file:
        return 0
    p = Path(count_file)
    n = int(p.read_text().strip()) if p.exists() else 0
    n += 1
    p.write_text(str(n))
    return n

if not argv:
    sys.exit(2)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    bump()
    pr = [{
        "number": int(os.environ.get("TEST_PR_NUMBER", "1259")),
        "title": "fix(pm-dispatch): LRU ordering within lanes",
        "updatedAt": "2026-07-10T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": os.environ.get("TEST_HEAD_SHA", "abc123def456"),
        "isDraft": False,
    }]
    print(json.dumps(pr))
    sys.exit(0)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] in ("issue", "run") and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "comment":
    sys.exit(0)

if argv[0] == "api":
    path = argv[1] if len(argv) > 1 else ""
    jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
    # Permission probe: bot can merge.
    if "repos/" in path and "permissions" in jq:
        print("true")
        sys.exit(0)
    # No prior bot comments (suppression must not fire).
    if "comments" in path:
        print("[]")
        sys.exit(0)
    if "notifications" in path:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def test_fresh_pr_detected_despite_stale_pr_data_cache() -> None:
    """Regression: a CLEAN+MERGEABLE PR opened after the pr-data cache write
    must be detected as merge_ready once the live cache expires.

    Setup: pre-populate the shared PR cache with [] (no open PRs) at a mtime
    150s ago. With GH_CACHE_TTL_PR=300 (5 min) the cache is still fresh for
    the pr-data lane, but with GH_CACHE_TTL_LIVE_PR=100 (shorter) it is stale
    for the live lane. The live fetch re-fetches and finds the PR.

    Before the fix: the gate skipped fetch_live_pr_data when pr_data=[], so
    live_pr_data stayed [] and check_merge_ready saw no PRs. The PR was
    invisible regardless of live cache expiry.

    After the fix: fetch_live_pr_data is always called. With the 100s live TTL
    and a 150s-old cache, it re-fetches and surfaces the PR.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-populate the shared PR cache with [] (simulates stale empty snapshot)
        cache_dir = state_dir / "gh-cache"
        cache_dir.mkdir()
        cache_file = cache_dir / f"pr-{TEST_REPO.replace('/', '-')}.json"
        cache_file.write_text("[]")

        # Set cache mtime to 150s ago: stale for live TTL (100s) but fresh for
        # cached TTL (300s). This is the exact scenario that caused contrib#1259.
        old_time = time.time() - 150
        os.utime(str(cache_file), (old_time, old_time))

        fake_gh = tmp / "gh"
        fake_gh.write_text(FAKE_GH_FRESH_PR)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        count_file = tmp / "pr-list-count.txt"

        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["TEST_PR_NUMBER"] = str(TEST_PR_NUMBER)
        env["TEST_HEAD_SHA"] = TEST_HEAD_SHA
        env["GH_PR_LIST_CALL_COUNT"] = str(count_file)
        # Cached TTL: 300s. Cache is 150s old -> HIT (still shows []).
        env["GH_CACHE_TTL_PR"] = "300"
        # Live TTL: 100s. Cache is 150s old -> MISS -> re-fetches -> finds PR.
        env["GH_CACHE_TTL_LIVE_PR"] = "100"

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

        assert result.returncode in (0, 1), result.stderr

        live_calls = int(count_file.read_text().strip()) if count_file.exists() else 0
        assert live_calls == 1, (
            f"live fetch must have fired to detect fresh PR (stale cache scenario); "
            f"got {live_calls} gh pr list call(s). "
            f"If 0, the gate still skips fetch_live_pr_data when pr_data=[]."
        )

        assert '"type":"merge_ready"' in result.stdout, (
            "fresh PR at CLEAN+MERGEABLE with no Greptile score (missing state file = OK) "
            "should be detected as merge_ready once the live cache expires, "
            f"but gate output was: {result.stdout!r}"
        )
