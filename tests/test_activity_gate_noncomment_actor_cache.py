"""The non-comment-bump actor probe must not re-run for an unchanged updatedAt.

When any item for a PR is dispatched and held, PM never promotes the pending
``pr-N.state`` watermark, so it rolls back every cycle. The "updatedAt moved
but no new comment" branch then re-ran its timelineItems GraphQL query for the
same updatedAt on every 2-4 min cycle (~17 calls/PR/hr on 2026-09-26). The
result is now cached per (repo, PR, updatedAt) in the persistent GH_CACHE_DIR.
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

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]

if argv[:2] == ["pr", "list"]:
    print(json.dumps([{
        "number": 7,
        "title": "feat: thing",
        "updatedAt": os.environ["TEST_UPDATED_AT"],
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": [],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "headRefOid": "a" * 40,
        "isDraft": False,
    }]))
    sys.exit(0)

if argv[:2] in (["issue", "list"], ["run", "list"]):
    print("[]")
    sys.exit(0)

if argv[:1] == ["api"]:
    if len(argv) > 1 and argv[1] == "graphql":
        if any("timelineItems" in a for a in argv):
            f = Path(os.environ["GH_COUNT_DIR"]) / "timeline"
            f.write_text(str(int(f.read_text()) + 1 if f.exists() else 1))
            # gh would apply --jq; emit the already-filtered actor login.
            print(os.environ.get("TEST_ACTOR", ""))
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


def _setup(tmp: Path) -> Path:
    fake = tmp / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    persistent = tmp / "state"
    persistent.mkdir()
    # Watermark older than updatedAt, no comments: the ambiguous non-comment bump.
    (persistent / "testorg-testrepo-pr-7.state").write_text("2026-09-25T00:00:00Z\n")
    return persistent


def _run_cycle(
    tmp: Path, persistent: Path, updated_at: str, actor: str
) -> subprocess.CompletedProcess[str]:
    """One PM cycle: reset pending from persistent, gate on pending, promote nothing."""
    pending = tmp / "pending"
    if pending.exists():
        shutil.rmtree(pending)
    shutil.copytree(persistent, pending)
    env = os.environ.copy()
    env["PATH"] = f"{tmp}:{env['PATH']}"
    env["GH_COUNT_DIR"] = str(tmp)
    env["TEST_UPDATED_AT"] = updated_at
    env["TEST_ACTOR"] = actor
    env["GH_CACHE_DIR"] = str(persistent / "gh-cache")
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
            "testorg/testrepo",
            "--state-dir",
            str(pending),
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _timeline_calls(tmp: Path) -> int:
    f = tmp / "timeline"
    return int(f.read_text()) if f.exists() else 0


def test_rolled_back_watermark_does_not_reprobe_same_updated_at() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        persistent = _setup(tmp)
        for _ in range(3):
            result = _run_cycle(tmp, persistent, "2026-09-26T00:00:00Z", "test-author")
            assert result.returncode in (0, 1), result.stderr
            assert '"type":"pr_update"' not in result.stdout, result.stdout
        assert _timeline_calls(tmp) == 1


def test_no_recognized_actor_is_cached_too() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        persistent = _setup(tmp)
        for _ in range(3):
            result = _run_cycle(tmp, persistent, "2026-09-26T00:00:00Z", "")
            assert result.returncode in (0, 1), result.stderr
        assert _timeline_calls(tmp) == 1


def test_new_updated_at_reprobes() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        persistent = _setup(tmp)
        _run_cycle(tmp, persistent, "2026-09-26T00:00:00Z", "test-author")
        _run_cycle(tmp, persistent, "2026-09-26T01:00:00Z", "test-author")
        assert _timeline_calls(tmp) == 2
