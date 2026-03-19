"""Tests for check_greptile_scores() API caching in activity-gate.sh.

Verifies that the 60-min HEAD-SHA-keyed cache correctly skips the
``gh api .../issues/.../comments`` REST call when the score is fresh,
and falls through to the API when the cache is stale or the HEAD SHA
has changed.
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
TEST_PR = 42
TEST_HEAD_SHA = "deadbeef1234"

# Fake gh stub: handles the minimal set of calls needed by activity-gate.sh.
# Counts hits to the issue-comments endpoint via GH_COMMENT_CALL_COUNT file.
FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess as sp, sys
from pathlib import Path

argv = sys.argv[1:]
head_sha  = os.environ.get("TEST_HEAD_SHA", "abc123")
pr_number = int(os.environ.get("TEST_PR_NUMBER", "42"))
count_file = os.environ.get("GH_COMMENT_CALL_COUNT", "")

def apply_jq(data: object, jq_expr: str) -> str:
    if not jq_expr:
        return json.dumps(data)
    r = sp.run(["jq", "-r", jq_expr],
               input=json.dumps(data), capture_output=True, text=True)
    return r.stdout.strip()

def parse_endpoint_and_jq(args: list[str]) -> tuple[str, str]:
    endpoint, jq_expr = "", ""
    i = 1
    while i < len(args):
        if args[i] == "--jq" and i + 1 < len(args):
            jq_expr = args[i + 1]; i += 2; continue
        if args[i] in ("-f", "-F", "-H", "-q") and i + 1 < len(args):
            i += 2; continue
        if args[i] in ("--paginate", "--silent"):
            i += 1; continue
        if args[i].startswith("--"):
            i += 1; continue
        if args[i] != "api":
            endpoint = args[i]
        i += 1
    return endpoint, jq_expr

if not argv:
    sys.exit(2)

# --- gh pr list ---
if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    pr = {
        "number": pr_number,
        "title": f"Test PR #{pr_number}",
        "updatedAt": "2026-01-01T00:00:00Z",
        "headRefOid": head_sha,
        "comments": {"nodes": []},
        "latestReviews": {"nodes": []},
        "statusCheckRollup": None,
        "mergeable": "UNKNOWN",
        "mergeStateStatus": "UNKNOWN",
        "author": {"login": "test-author"},
    }
    print(json.dumps([pr]))
    sys.exit(0)

# --- gh api ... ---
if argv[0] == "api":
    endpoint, jq_expr = parse_endpoint_and_jq(argv)

    # Issue comments endpoint — the one we're caching
    if f"/issues/{pr_number}/comments" in endpoint:
        if count_file:
            p = Path(count_file)
            n = int(p.read_text().strip()) if p.exists() else 0
            p.write_text(str(n + 1))
        comments = [{
            "user": {"login": "greptile-apps[bot]"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "body": "<h3>Greptile Summary</h3>\nScore: 4/5\nSome findings.",
        }]
        print(apply_jq(comments, jq_expr))
        sys.exit(0)

    # Notifications — return 0 so check_notifications doesn't block
    if "notifications" in endpoint:
        print("0")
        sys.exit(0)

    # All other API calls (CI status, reactions, etc.) — return empty
    print("[]")
    sys.exit(0)

# Everything else (gh pr comment, gh issue list, …) — succeed silently
sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    head_sha: str = TEST_HEAD_SHA,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run activity-gate.sh with fake gh. Returns (result, comment_api_call_count)."""
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    count_file = tmp / "comment-count.txt"

    env = os.environ.copy()
    env["TEST_HEAD_SHA"] = head_sha
    env["TEST_PR_NUMBER"] = str(TEST_PR)
    env["GH_COMMENT_CALL_COUNT"] = str(count_file)
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


def _state_file(state_dir: Path) -> Path:
    repo_safe = TEST_REPO.replace("/", "-")
    return state_dir / f"{repo_safe}-pr-{TEST_PR}-greptile.state"


def test_fresh_cache_skips_api_call() -> None:
    """Cache hit: same head_sha, timestamp 5 min ago → skip gh api call."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-populate: score 4, 5 min ago, same head_sha
        ts = int(time.time()) - 300
        _state_file(state_dir).write_text(f"4:{ts}:{TEST_HEAD_SHA}")

        result, call_count = _run_gate(tmp, state_dir)
        assert result.returncode in (
            0,
            1,
        ), f"Script crashed (rc={result.returncode}): {result.stderr}"
        assert (
            call_count == 0
        ), f"Expected 0 API calls (cache hit, same SHA, fresh), got {call_count}"


def test_new_head_sha_calls_api() -> None:
    """Cache miss: head_sha changed → must fetch from API."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-populate with a DIFFERENT head_sha (5 min ago)
        ts = int(time.time()) - 300
        _state_file(state_dir).write_text(f"4:{ts}:oldsha999")

        result, call_count = _run_gate(tmp, state_dir, head_sha=TEST_HEAD_SHA)
        assert result.returncode in (
            0,
            1,
        ), f"Script crashed (rc={result.returncode}): {result.stderr}"
        assert (
            call_count >= 1
        ), f"Expected API call (cache miss: SHA mismatch), got {call_count}"


def test_stale_cache_calls_api() -> None:
    """Cache miss: same head_sha but timestamp > 60 min → re-fetch from API."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Pre-populate: same head_sha, but 65 minutes ago (> 60-min TTL)
        ts = int(time.time()) - (65 * 60)
        _state_file(state_dir).write_text(f"4:{ts}:{TEST_HEAD_SHA}")

        result, call_count = _run_gate(tmp, state_dir)
        assert result.returncode in (
            0,
            1,
        ), f"Script crashed (rc={result.returncode}): {result.stderr}"
        assert (
            call_count >= 1
        ), f"Expected API call (cache miss: stale TTL), got {call_count}"


def test_no_state_file_calls_api() -> None:
    """No prior state → first-time seed, always calls API."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        # No state file — first-time encounter

        result, call_count = _run_gate(tmp, state_dir)
        assert result.returncode in (
            0,
            1,
        ), f"Script crashed (rc={result.returncode}): {result.stderr}"
        assert call_count >= 1, f"Expected API call (no state file), got {call_count}"
        # State file should be seeded now
        assert _state_file(
            state_dir
        ).exists(), "State file should be created on first run"
