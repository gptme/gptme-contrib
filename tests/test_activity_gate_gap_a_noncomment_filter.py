"""Regression tests for Gap A in activity-gate.sh.

Gap A (open since 2026-05-23): activity-gate.sh filters self-triggered updates by
checking the latest comment/review actor, but non-comment events (push, draft toggle,
label/assignee change) bump updatedAt without creating a comment. If the latest
comment pre-dates the watermark but was from an external actor (e.g., Greptile),
the gate incorrectly emits pr_update — dispatching a no-op PM session.

Fix: when updatedAt > watermark but the latest comment/review timestamp is ≤
watermark, fetch timelineItems(last: 3) via GraphQL to find the actual actor.
Suppress if the actor is AUTHOR. On error, fall through to existing emit behavior.
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
TEST_PR = 99

# Timeline of events:
#   COMMENT_TIME  < WATERMARK_TIME  < PUSH_TIME
# Greptile commented at COMMENT_TIME, we processed it at WATERMARK_TIME
# (storing that as last_check).  AUTHOR/external-actor pushes at PUSH_TIME.
COMMENT_TIME = "2026-07-10T09:00:00Z"  # old Greptile comment
WATERMARK_TIME = "2026-07-10T10:00:00Z"  # stored last_check in state file
PUSH_TIME = "2026-07-10T12:00:00Z"  # updatedAt after push (> watermark)

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys

argv = sys.argv[1:]
pr_number = int(os.environ.get("TEST_PR_NUMBER", "99"))
comment_time = os.environ.get("COMMENT_TIME", "2026-07-10T09:00:00Z")
push_time = os.environ.get("PUSH_TIME", "2026-07-10T12:00:00Z")
# The actor returned by gh api graphql (timelineItems).
# Empty string simulates an API error / no-events-found path.
timeline_actor = os.environ.get("TIMELINE_ACTOR", "")

if not argv:
    sys.exit(2)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    pr = [{
        "number": pr_number,
        "title": f"fix: some improvement #{pr_number}",
        "updatedAt": push_time,
        "comments": [{
            "author": {"login": "greptile-apps"},
            "createdAt": comment_time,
            "body": "Score: 4/5",
        }],
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BEHIND",
        "headRefOid": "abc123feed",
        "isDraft": False,
    }]
    print(json.dumps(pr))
    sys.exit(0)

if argv[0] in ("issue", "run") and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "comment":
    sys.exit(0)

if argv[0] == "api" and len(argv) > 1 and argv[1] == "graphql":
    # fetch_pr_noncomment_actor: print the login (what --jq extracts), or nothing.
    if timeline_actor:
        print(timeline_actor)
    sys.exit(0)

if argv[0] == "api":
    path = argv[1] if len(argv) > 1 else ""
    jq = argv[argv.index("--jq") + 1] if "--jq" in argv else ""
    if "permissions" in jq:
        print("true")
        sys.exit(0)
    if "notifications" in path:
        sys.exit(0)
    if "--jq" in argv:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _state_file(state_dir: Path) -> Path:
    return state_dir / f"{TEST_REPO.replace('/', '-')}-pr-{TEST_PR}.state"


def _run_gate(
    tmp: Path, state_dir: Path, *, timeline_actor: str
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_PR_NUMBER"] = str(TEST_PR)
    env["COMMENT_TIME"] = COMMENT_TIME
    env["PUSH_TIME"] = PUSH_TIME
    env["TIMELINE_ACTOR"] = timeline_actor
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


def test_own_push_after_external_comment_does_not_emit_pr_update() -> None:
    """Gap A fix: AUTHOR's own push must NOT trigger a pr_update dispatch.

    Scenario:
      T=09:00 — Greptile comments (score 4/5)
      T=10:00 — gate processed, watermark stored as last_check
      T=12:00 — AUTHOR pushes, updatedAt bumps (no new comment)

    Next gate run: updatedAt(12:00) > watermark(10:00). Latest comment is Greptile
    at 09:00 ≤ watermark → ambiguous bump. timelineItems returns AUTHOR → suppress.

    Before the fix: has_actionable_update saw Greptile (not AUTHOR) as last actor
    and emitted pr_update, dispatching a wasted no-op PM session.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        _state_file(state_dir).write_text(WATERMARK_TIME)

        result = _run_gate(tmp, state_dir, timeline_actor="test-author")

        assert result.returncode in (0, 1), result.stderr
        assert '"type":"pr_update"' not in result.stdout, (
            "Gap A: AUTHOR's own push bumped updatedAt. Latest comment (Greptile) "
            "predates the watermark. timelineItems returned AUTHOR as actor. "
            f"pr_update must be suppressed. Output: {result.stdout!r}\n"
            f"Stderr: {result.stderr!r}"
        )


def test_external_push_after_external_comment_still_emits_pr_update() -> None:
    """Non-AUTHOR bump must still emit pr_update (safe failure direction).

    Same setup, but the push was by an external collaborator, not AUTHOR.
    The gate must NOT suppress — external activity is actionable.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        _state_file(state_dir).write_text(WATERMARK_TIME)

        result = _run_gate(tmp, state_dir, timeline_actor="external-collaborator")

        assert result.returncode in (0, 1), result.stderr
        assert '"type":"pr_update"' in result.stdout, (
            "External actor bumped updatedAt. Gate must still emit pr_update. "
            f"Output: {result.stdout!r}\n"
            f"Stderr: {result.stderr!r}"
        )


def test_fetch_pr_noncomment_actor_includes_regular_push_event() -> None:
    """P1 regression: PULL_REQUEST_COMMIT must be in the itemTypes list.

    HEAD_REF_FORCE_PUSHED_EVENT only covers force pushes.  A normal `git push`
    creates PULL_REQUEST_COMMIT timeline items — without this type in the query
    the helper returns empty for every regular push, leaving the common
    self-push path unsuppressed.
    """
    content = SCRIPT.read_text()
    assert "PULL_REQUEST_COMMIT" in content, (
        "fetch_pr_noncomment_actor must include PULL_REQUEST_COMMIT in itemTypes "
        "to detect regular (non-force) pushes. Without it, normal git pushes by "
        "the author return an empty actor and fall through to emit pr_update."
    )


def test_fetch_pr_noncomment_actor_includes_metadata_removal_events() -> None:
    """P2 regression: metadata-removal event types must be in the itemTypes list.

    ASSIGNED_EVENT is present but UNASSIGNED_EVENT, REVIEW_REQUESTED_EVENT, and
    REVIEW_REQUEST_REMOVED_EVENT were missing.  Author-triggered metadata removals
    bump updatedAt without a comment, causing spurious pr_update dispatches.
    """
    content = SCRIPT.read_text()
    for event_type in (
        "UNASSIGNED_EVENT",
        "REVIEW_REQUESTED_EVENT",
        "REVIEW_REQUEST_REMOVED_EVENT",
    ):
        assert event_type in content, (
            f"fetch_pr_noncomment_actor must include {event_type} in itemTypes. "
            "Missing metadata-removal events leave author-triggered updatedAt "
            "bumps uncovered, producing spurious pr_update dispatches."
        )


def test_timeline_api_error_falls_through_to_emit() -> None:
    """On timeline API error (empty return), fall through to existing emit behavior.

    When fetch_pr_noncomment_actor returns empty, the gate must not silently drop
    potentially-external activity — emit pr_update as before the fix.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        _state_file(state_dir).write_text(WATERMARK_TIME)

        # TIMELINE_ACTOR="" → fake gh returns nothing → empty actor → fall through
        result = _run_gate(tmp, state_dir, timeline_actor="")

        assert result.returncode in (0, 1), result.stderr
        assert '"type":"pr_update"' in result.stdout, (
            "Timeline API returned empty (error). Gate must fall through and emit "
            f"pr_update. Output: {result.stdout!r}\n"
            f"Stderr: {result.stderr!r}"
        )
