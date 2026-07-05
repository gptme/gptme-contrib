"""Regression tests for own-PR Greptile review detection in activity-gate.sh."""

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
TEST_PR = 42
TEST_HEAD_SHA = "deadbeef1234"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys

argv = sys.argv[1:]
pr_number = int(os.environ.get("TEST_PR_NUMBER", "42"))
head_sha = os.environ.get("TEST_HEAD_SHA", "abc123")
merge_state = os.environ.get("TEST_MERGE_STATE", "BEHIND")

if not argv:
    sys.exit(2)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    pr = [{
        "number": pr_number,
        "title": f"Test PR #{pr_number}",
        "updatedAt": "2026-06-12T00:00:00Z",
        "comments": [],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge_state,
        "headRefOid": head_sha,
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

if argv[0] == "api" and len(argv) > 1 and argv[1] == "notifications":
    sys.exit(0)

if argv[0] == "api":
    # When --jq is passed, simulate real jq-filtered output: no greptile comments
    # means the filter produces empty output (not the raw "[]" array).
    if "--jq" in argv:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _greptile_state_file(state_dir: Path) -> Path:
    repo_safe = TEST_REPO.replace("/", "-")
    return state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"


def _own_review_state_file(state_dir: Path) -> Path:
    repo_safe = TEST_REPO.replace("/", "-")
    return state_dir / f"{repo_safe}-pr-{TEST_PR}-own-pr-review.state"


def _run_gate(
    tmp: Path, state_dir: Path, *, merge_state: str
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_PR_NUMBER"] = str(TEST_PR)
    env["TEST_HEAD_SHA"] = TEST_HEAD_SHA
    env["TEST_MERGE_STATE"] = merge_state
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
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_stale_greptile_sha_does_not_emit_for_new_head() -> None:
    """Greptile state for an old SHA must not dispatch against the new HEAD."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        old_sha = "oldsha0000"
        ts = int(time.time()) - 60
        # State file records a review for old_sha, but live PR is at TEST_HEAD_SHA
        _greptile_state_file(state_dir).write_text(f"3:{ts}:{old_sha}")

        result = _run_gate(tmp, state_dir, merge_state="CLEAN")
        assert result.returncode in (0, 1), result.stderr
        # check_own_pr_review_state must NOT emit — checked via the specific detail string
        # and the absence of its state file. (check_greptile_scores may emit separately
        # for the SHA change; that's expected and not what this test covers.)
        assert "own-PR review" not in result.stdout, result.stdout
        assert not _own_review_state_file(state_dir).exists()


def test_perfect_greptile_review_does_not_emit_improvement_for_behind_pr() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        ts = int(time.time()) - 60
        _greptile_state_file(state_dir).write_text(f"5:{ts}:{TEST_HEAD_SHA}")

        result = _run_gate(tmp, state_dir, merge_state="BEHIND")
        assert result.returncode in (0, 1), result.stderr
        assert "greptile_needs_improvement" not in result.stdout, result.stdout
        assert "greptile_needs_fix" not in result.stdout, result.stdout
        assert not _own_review_state_file(state_dir).exists()


def test_blocked_pr_with_low_greptile_emits_improvement() -> None:
    """A branch-protected green PR sits in BLOCKED while awaiting required review.

    A sub-5 Greptile score there is actionable, so check_own_pr_review_state must
    emit on first discovery rather than waiting for the 1h check_greptile_scores
    cooldown nag (the latency window that forced manual @-mentions).
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        ts = int(time.time()) - 60
        # Greptile 4/5 already on file for the current HEAD.
        _greptile_state_file(state_dir).write_text(f"4:{ts}:{TEST_HEAD_SHA}")

        result = _run_gate(tmp, state_dir, merge_state="BLOCKED")
        assert result.returncode in (0, 1), result.stderr
        assert "greptile_needs_improvement" in result.stdout, result.stdout
        # The own-PR review path is what fired (identified by its detail string).
        assert "own-PR review" in result.stdout, result.stdout
        assert _own_review_state_file(state_dir).exists()


def test_unknown_merge_state_still_skips_own_pr_review() -> None:
    """UNKNOWN means GitHub is still computing mergeability — stay transient-safe."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        ts = int(time.time()) - 60
        _greptile_state_file(state_dir).write_text(f"4:{ts}:{TEST_HEAD_SHA}")

        result = _run_gate(tmp, state_dir, merge_state="UNKNOWN")
        assert result.returncode in (0, 1), result.stderr
        assert "own-PR review" not in result.stdout, result.stdout
        assert not _own_review_state_file(state_dir).exists()
