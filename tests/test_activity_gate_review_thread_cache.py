"""The merge_ready review-thread probe must not re-run every gate cycle.

PM keeps a dispatched item's merge-ready cooldown stamp in a pending state dir
that is reset from the persistent dir every cycle. A merge_ready item the
dispatcher holds is never promoted, so the 12h cooldown never arms and the
reviewThreads GraphQL probe re-ran for every held PR on every cycle (~480
calls/hr on 2026-09-26). The probe result is now cached per (repo, PR, HEAD)
in the persistent GH_CACHE_DIR.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

TEST_REPO = "testorg/testrepo"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]

def bump(name: str) -> None:
    f = Path(os.environ["GH_COUNT_DIR"]) / name
    n = int(f.read_text()) if f.exists() else 0
    f.write_text(str(n + 1))

if argv[:2] == ["pr", "list"]:
    print(json.dumps([{
        "number": 7,
        "title": "feat: thing",
        "updatedAt": "2026-09-26T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": [],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": os.environ["TEST_HEAD_SHA"],
        "isDraft": False,
    }]))
    sys.exit(0)

if argv[:2] in (["issue", "list"], ["run", "list"]):
    print("[]")
    sys.exit(0)

if argv[:1] == ["api"]:
    if len(argv) > 1 and argv[1] == "graphql":
        if any("reviewThreads" in a for a in argv):
            bump("review-threads")
            print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }}}}}))
            sys.exit(0)
        print("{}")
        sys.exit(0)
    jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
    if "permissions" in jq:
        print("true")
        sys.exit(0)
    if "notifications" in argv[1]:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_cycle(
    tmp: Path, persistent: Path, head: str
) -> subprocess.CompletedProcess[str]:
    """One PM cycle: reset pending from persistent, run the gate on pending.

    Nothing is promoted back, which is what happens to a dispatched-but-held
    merge_ready item.
    """
    pending = tmp / "pending"
    if pending.exists():
        shutil.rmtree(pending)
    shutil.copytree(persistent, pending)
    env = os.environ.copy()
    env["PATH"] = f"{tmp}:{env['PATH']}"
    env["GH_COUNT_DIR"] = str(tmp)
    env["TEST_HEAD_SHA"] = head
    env["GH_CACHE_DIR"] = str(persistent / "gh-cache")
    # PR lists shares GH_CACHE_DIR; bypass it so a head change is visible.
    env["GH_CACHE_TTL_PR"] = "0"
    env["GH_CACHE_TTL_LIVE_PR"] = "0"
    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--org",
            "testorg",
            "--repo",
            TEST_REPO,
            "--state-dir",
            str(pending),
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _thread_calls(tmp: Path) -> int:
    f = tmp / "review-threads"
    return int(f.read_text()) if f.exists() else 0


def test_held_merge_ready_does_not_reprobe_threads_each_cycle() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        fake = tmp / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        persistent = tmp / "state"
        persistent.mkdir()

        for _ in range(3):
            result = _run_cycle(tmp, persistent, "a" * 40)
            assert result.returncode in (0, 1), result.stderr
            assert '"type":"merge_ready"' in result.stdout, result.stdout

        assert _thread_calls(tmp) == 1


def test_new_head_reprobes_threads() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        fake = tmp / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        persistent = tmp / "state"
        persistent.mkdir()

        _run_cycle(tmp, persistent, "a" * 40)
        _run_cycle(tmp, persistent, "b" * 40)

        assert _thread_calls(tmp) == 2
