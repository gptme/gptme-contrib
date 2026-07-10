"""Regression tests for merge-permission suppression in activity-gate.sh.

A merge-ready PR on a repo where the bot has pull-only access (e.g. an external
upstream contributed to via a fork) can NEVER be self-merged, so dispatching a
monitoring session for it is always a NOOP. check_merge_ready must instead post a
one-time maintainer-waiting comment (surfacing the ready PR to a human and arming
has_maintainer_waiting_comment) and suppress the emit. On a repo the bot CAN
merge, merge_ready must still emit as normal.
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
TEST_PR = 42
TEST_HEAD_SHA = "deadbeef1234"

# Fake gh: drives check_merge_ready. Permission is env-controlled (TEST_CAN_MERGE),
# the PR is CLEAN/MERGEABLE, there are no prior bot comments (so the existing
# comment-suppression does not fire), and `pr comment` records to COMMENT_MARKER.
FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import os, sys, json

argv = sys.argv[1:]
repo = os.environ.get("TEST_REPO", "testorg/testrepo")
pr_number = int(os.environ.get("TEST_PR_NUMBER", "42"))
head_sha = os.environ.get("TEST_HEAD_SHA", "abc123")
can_merge = os.environ.get("TEST_CAN_MERGE", "true")
marker = os.environ.get("COMMENT_MARKER", "")

if not argv:
    sys.exit(2)

# `gh pr comment <n> --repo R --body ...` — record that a comment was posted.
if argv[0] == "pr" and len(argv) > 1 and argv[1] == "comment":
    if marker:
        with open(marker, "a") as fh:
            fh.write("comment:%s\n" % (argv[2] if len(argv) > 2 else "?"))
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    pr = [{
        "number": pr_number,
        "title": "Test PR #%d" % pr_number,
        "updatedAt": "2026-06-12T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": head_sha,
        "isDraft": False,
    }]
    print(json.dumps(pr))
    sys.exit(0)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] in ("issue", "run") and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "api" and len(argv) > 1 and argv[1] == "notifications":
    sys.exit(0)

if argv[0] == "api" and len(argv) > 1:
    path = argv[1]
    jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
    # Permission probe: `gh api repos/<repo> --jq '.permissions | ...'`
    if path == "repos/" + repo and "permissions" in jq:
        print(can_merge)
        sys.exit(0)
    # Bot comment history (has_maintainer_waiting_comment) — no prior comments.
    if "comments" in path:
        sys.exit(0)
    if "--jq" in argv:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    *,
    can_merge: str,
    marker: Path,
    fmt: str = "jsonl",
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_REPO"] = TEST_REPO
    env["TEST_PR_NUMBER"] = str(TEST_PR)
    env["TEST_HEAD_SHA"] = TEST_HEAD_SHA
    env["TEST_CAN_MERGE"] = can_merge
    env["COMMENT_MARKER"] = str(marker)
    env["PATH"] = f"{tmp}:{env['PATH']}"

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
            str(state_dir),
            "--format",
            fmt,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_pull_only_repo_suppresses_merge_ready_and_posts_comment() -> None:
    """Bot lacks merge permission → no merge_ready emit, one maintainer comment."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        marker = tmp / "comment_marker"

        result = _run_gate(tmp, state_dir, can_merge="false", marker=marker)
        assert result.returncode in (0, 1), result.stderr
        assert "merge_ready" not in result.stdout, result.stdout
        assert marker.exists(), "expected a maintainer-waiting comment to be posted"
        assert marker.read_text().count("comment:") == 1, marker.read_text()


def test_mergeable_repo_still_emits_merge_ready() -> None:
    """Bot has merge permission → merge_ready emits, no comment posted."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        marker = tmp / "comment_marker"

        result = _run_gate(tmp, state_dir, can_merge="true", marker=marker)
        assert result.returncode in (0, 1), result.stderr
        assert "merge_ready" in result.stdout, result.stdout
        assert not marker.exists(), "no maintainer comment should be posted"


def test_markdown_mode_does_not_write_state_file() -> None:
    """In markdown preview mode, pull-only repos must NOT write the state file.

    If the state file were written during a markdown pass, the subsequent jsonl
    dispatch pass (the one that actually posts comments and emits items) would
    see the state file within its 12 h cooldown window and skip the PR entirely
    — the maintainer-waiting comment would never be posted.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        marker = tmp / "comment_marker"

        # First pass: markdown mode (preview) — pull-only repo
        result = _run_gate(
            tmp, state_dir, can_merge="false", marker=marker, fmt="markdown"
        )
        assert result.returncode in (0, 1), result.stderr
        assert "merge_ready" not in result.stdout

        # No comment should be posted in markdown mode
        assert not marker.exists(), "no comment should be posted in markdown mode"

        # No state file should be written — the next jsonl pass must still fire
        state_files = list(state_dir.glob("*-merge-ready.state"))
        assert not state_files, f"state file written during markdown pass; next jsonl run would skip the PR: {state_files}"

        # Second pass: jsonl mode — NOW the comment should be posted
        result2 = _run_gate(
            tmp, state_dir, can_merge="false", marker=marker, fmt="jsonl"
        )
        assert result2.returncode in (0, 1), result2.stderr
        assert "merge_ready" not in result2.stdout
        assert marker.exists(), "jsonl run must post the maintainer-waiting comment"
        assert marker.read_text().count("comment:") == 1, marker.read_text()
