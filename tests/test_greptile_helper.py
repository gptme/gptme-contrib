"""Black-box tests for scripts/github/greptile-helper.sh.

The fake gh stub returns raw GitHub API response arrays and applies --jq
via real jq, so the script's jq expressions (score regex, date filtering,
sort ordering) are exercised by every test.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, overload

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "greptile-helper.sh"

# Fake gh stub: returns raw API data, applies --jq via real jq binary.
FAKE_GH = r'''#!/usr/bin/env python3
"""Fake gh CLI that returns raw GitHub API responses and applies --jq via jq."""
from pathlib import Path
import json
import os
import subprocess as sp
import sys

fixture = json.loads(Path(os.environ["GH_FIXTURE"]).read_text())
argv = sys.argv[1:]

if argv[:2] == ["pr", "comment"]:
    Path(os.environ["GH_LOG"]).write_text(json.dumps(argv))
    raise SystemExit(0)

if not argv or argv[0] != "api":
    raise SystemExit(2)

# Parse flags
endpoint = ""
jq_expr = ""
fields: dict = {}
i = 1
while i < len(argv):
    arg = argv[i]
    if arg == "--jq":
        jq_expr = argv[i + 1]; i += 2; continue
    if arg in ("-f", "-F") and i + 1 < len(argv):
        kv = argv[i + 1]
        if "=" in kv:
            k, v = kv.split("=", 1)
            fields[k] = v
        i += 2; continue
    if arg in ("-q", "-H"):
        i += 2; continue
    if arg == "--paginate":
        i += 1; continue
    if arg.startswith("-"):
        i += 1; continue
    endpoint = arg; i += 1

github_author = os.environ.get("GITHUB_AUTHOR", "test-user")
pr_number = fixture["pr_number"]

# REST POST to comments endpoint (script uses gh api instead of gh pr comment)
if "body" in fields and endpoint.endswith(f"/issues/{pr_number}/comments"):
    Path(os.environ["GH_LOG"]).write_text(json.dumps({"body": fields["body"]}))
    raise SystemExit(0)

# Route to fixture data
data = None
if endpoint == "user":
    print(github_author)
    raise SystemExit(0)
elif endpoint.endswith(f"/issues/{pr_number}/comments"):
    data = fixture.get("raw_comments", [])
elif endpoint.endswith(f"/pulls/{pr_number}/commits"):
    data = fixture.get("raw_commits", [])
elif endpoint.endswith(f"/pulls/{pr_number}"):
    data = fixture.get("raw_pr", {"created_at": "2020-01-01T00:00:00Z"})
elif endpoint.endswith("/reactions"):
    if fixture.get("reactions_error"):
        raise SystemExit(1)
    data = [{"user": {"login": "greptile-apps[bot]"}}] * fixture.get("bot_reaction_count", 0)
else:
    raise SystemExit(4)

raw = json.dumps(data)
if jq_expr:
    # Use jq -r to match gh --jq behavior (outputs raw strings, not quoted)
    r = sp.run(["jq", "-r", jq_expr], input=raw, capture_output=True, text=True)
    print(r.stdout.strip())
else:
    print(raw)
'''


def _iso_ago(*, minutes: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_greptile_comment(
    score: int, reviewed_at: str, updated_at: str | None = None
) -> dict:
    return {
        "user": {"login": "greptile-apps[bot]"},
        "created_at": reviewed_at,
        "updated_at": updated_at or reviewed_at,
        "body": f"<h3>Greptile Summary</h3>\nFindings.\n<h3>Confidence Score: {score}/5</h3>\nDetails.",
    }


def _make_trigger_comment(author: str, created_at: str) -> dict:
    return {
        "id": 12345,
        "user": {"login": author},
        "created_at": created_at,
        "updated_at": created_at,
        "body": "@greptileai review",
    }


def _make_commit(date: str) -> dict:
    return {
        "sha": "abc123",
        "commit": {
            "author": {"date": date},
            "committer": {"date": date},
            "message": "test commit",
        },
    }


def _pr_hash(repo: str, pr_number: int) -> str:
    """Compute the per-PR hash used for lock and trigger-timestamp file names.

    Mirrors the shell: printf '%s#%s' "$REPO" "$PR_NUMBER" | md5sum | cut -c1-12
    """
    return hashlib.md5(f"{repo}#{pr_number}".encode()).hexdigest()[:12]


@overload
def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: Literal[False] = ...,
    pre_trigger_ts: str | None = ...,
    repo: str = ...,
) -> subprocess.CompletedProcess[str]: ...


@overload
def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: Literal[True],
    pre_trigger_ts: str | None = ...,
    repo: str = ...,
) -> tuple[subprocess.CompletedProcess[str], str]: ...


def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: bool = False,
    pre_trigger_ts: str | None = None,
    repo: str = "gptme/gptme",
) -> subprocess.CompletedProcess[str] | tuple[subprocess.CompletedProcess[str], str]:
    """Run greptile-helper.sh with a fake gh stub.

    Args:
        command: check | trigger | status
        fixture: fake API data for the gh stub
        capture_gh_log: if True, returns (result, gh_log_content) tuple
        pre_trigger_ts: if set, pre-create the local trigger-timestamp file with
            this timestamp (simulates a prior successful trigger in the same TMPDIR)
        repo: repo string passed to the helper (default "gptme/gptme")
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))

        fake_gh = tmp_path / "gh"
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        # Pre-seed the trigger-timestamp file if the test needs it
        if pre_trigger_ts is not None:
            pr_number = int(str(fixture["pr_number"]))
            ts_file = tmp_path / f"greptile-trigger-ts-{_pr_hash(repo, pr_number)}.txt"
            ts_file.write_text(pre_trigger_ts)

        gh_log = tmp_path / "gh-log.json"
        env = os.environ.copy()
        env["GH_FIXTURE"] = str(fixture_path)
        env["GH_LOG"] = str(gh_log)
        env["GITHUB_AUTHOR"] = "test-user"
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["TMPDIR"] = tmp  # ensure helper writes state files to test's temp dir

        result = subprocess.run(
            ["bash", str(SCRIPT), command, repo, str(fixture["pr_number"])],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if capture_gh_log:
            log_content = gh_log.read_text() if gh_log.exists() else ""
            return result, log_content
        return result


def test_check_blocks_recently_acknowledged_trigger():
    """Trigger < 20min with bot ack → in-progress."""
    fixture = {
        "pr_number": 123,
        "raw_comments": [
            _make_trigger_comment("test-user", _iso_ago(minutes=16)),
        ],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 1,
    }
    result = _run_helper("check", fixture)
    assert result.returncode == 1, f"stderr: {result.stderr}"

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "in-progress"


def test_check_retries_acknowledged_trigger_after_timeout():
    """Stale trigger (> 20min, bot ack, no review) → still awaiting-initial-review; check skips."""
    fixture = {
        "pr_number": 123,
        "raw_comments": [
            _make_trigger_comment("test-user", _iso_ago(minutes=25)),
        ],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 1,
    }
    result = _run_helper("check", fixture)
    assert (
        result.returncode == 1
    ), f"Should skip (no review yet, never trigger initial). stderr: {result.stderr}"

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "awaiting-initial-review"


def test_rereview_ignores_old_trigger_from_previous_review_cycle():
    """Score 4/5 + new commits since review → needs-re-review."""
    reviewed_at = _iso_ago(minutes=30)
    fixture = {
        "pr_number": 123,
        "raw_comments": [
            _make_greptile_comment(
                4, reviewed_at=_iso_ago(minutes=60), updated_at=reviewed_at
            ),
            _make_trigger_comment("test-user", _iso_ago(minutes=45)),
        ],
        "raw_commits": [
            _make_commit(_iso_ago(minutes=10)),
        ],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 1,
    }
    result = _run_helper("check", fixture)
    assert result.returncode == 0, f"stderr: {result.stderr}"

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "needs-re-review"


def test_fresh_pr_awaits_initial_review():
    """Fresh PR (< 20 min, no review, no trigger) → awaiting-initial-review."""
    fixture = {
        "pr_number": 456,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=5)},
        "bot_reaction_count": 0,
    }
    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "awaiting-initial-review"

    result = _run_helper("check", fixture)
    assert result.returncode == 1, f"Should skip fresh PR. stderr: {result.stderr}"


def test_old_pr_without_review_awaits_initial_review():
    """Old PR (> 20 min, no review, no trigger) → awaiting-initial-review (never trigger initial)."""
    fixture = {
        "pr_number": 789,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=60)},
        "bot_reaction_count": 0,
    }
    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "awaiting-initial-review"

    result = _run_helper("check", fixture)
    assert (
        result.returncode == 1
    ), f"Should skip (never trigger initial review). stderr: {result.stderr}"


def test_score_5_is_already_reviewed():
    """Score 5/5 → already-reviewed."""
    fixture = {
        "pr_number": 123,
        "raw_comments": [
            _make_greptile_comment(5, reviewed_at=_iso_ago(minutes=30)),
        ],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 0,
    }
    result = _run_helper("check", fixture)
    assert result.returncode == 2, f"stderr: {result.stderr}"

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "already-reviewed"


def test_trigger_skips_unreviewed_pr_initial_review():
    """Trigger on old PR with no review → skips (awaiting Greptile auto-review)."""
    fixture = {
        "pr_number": 999,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=60)},
        "bot_reaction_count": 0,
    }
    result, gh_log = _run_helper("trigger", fixture, capture_gh_log=True)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Awaiting" in result.stdout
    assert not gh_log, "gh pr comment should NOT have been called"


def test_trigger_skips_fresh_pr():
    """Trigger on fresh PR (< 20 min) → skips, waits for auto-review."""
    fixture = {
        "pr_number": 888,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=5)},
        "bot_reaction_count": 0,
    }
    result = _run_helper("trigger", fixture)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Awaiting" in result.stdout


def test_trigger_re_reviews_on_low_score_with_new_commits():
    """Trigger on PR with score 4/5 + new commits → posts re-review comment."""
    reviewed_at = _iso_ago(minutes=30)
    fixture = {
        "pr_number": 777,
        "raw_comments": [
            _make_greptile_comment(
                4, reviewed_at=_iso_ago(minutes=60), updated_at=reviewed_at
            ),
            # Old trigger from previous cycle (before review)
            _make_trigger_comment("test-user", _iso_ago(minutes=45)),
        ],
        "raw_commits": [
            _make_commit(_iso_ago(minutes=10)),  # New commit after review
        ],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 1,
    }
    result, gh_log = _run_helper("trigger", fixture, capture_gh_log=True)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Re-triggered successfully" in result.stdout
    assert (
        gh_log
    ), "comment was never posted (neither gh pr comment nor gh api REST call)"
    assert "@greptileai review" in gh_log


def test_max_retries_guard_blocks_after_repeated_triggers():
    """3 triggers since last review (Greptile acked but never reviewed) → blocked.

    Regression test for gptme#1651 (2026-03-18): 7 @greptileai review comments
    posted in one day because the 20-min ACK_GRACE_SECONDS expiry kept marking
    prior triggers as 'stale' even though Greptile had acked each one.
    """
    reviewed_at = _iso_ago(minutes=120)
    fixture = {
        "pr_number": 1651,
        "raw_comments": [
            _make_greptile_comment(4, reviewed_at=reviewed_at),
            # Three re-review triggers posted after the review, none responded to
            _make_trigger_comment("test-user", _iso_ago(minutes=90)),
            _make_trigger_comment("test-user", _iso_ago(minutes=60)),
            _make_trigger_comment("test-user", _iso_ago(minutes=30)),
        ],
        "raw_commits": [
            _make_commit(_iso_ago(minutes=10)),  # New commits since review
        ],
        "raw_pr": {"created_at": _iso_ago(minutes=180)},
        "bot_reaction_count": 1,  # Greptile acks each trigger but never posts a review
    }
    # check: should block (max retries reached)
    result = _run_helper("check", fixture)
    assert (
        result.returncode == 1
    ), f"Should block after max retries. stderr: {result.stderr}"

    # status: should report in-progress (trigger loop blocked)
    status = _run_helper("status", fixture)
    assert (
        status.stdout.strip() == "in-progress"
    ), f"Expected in-progress, got: {status.stdout.strip()}"

    # trigger: should NOT post a new comment
    result, gh_log = _run_helper("trigger", fixture, capture_gh_log=True)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert not gh_log, f"Should NOT have posted trigger comment, got: {gh_log}"


def test_max_retries_guard_allows_below_threshold():
    """2 triggers since last review (< MAX_RE_TRIGGERS=3) → NOT blocked, should re-trigger.

    Lower-bound boundary check: the guard only fires at >= threshold, not below.
    """
    reviewed_at = _iso_ago(minutes=120)
    fixture = {
        "pr_number": 1651,
        "raw_comments": [
            _make_greptile_comment(4, reviewed_at=reviewed_at),
            # Two re-review triggers posted after the review (below the threshold of 3)
            _make_trigger_comment("test-user", _iso_ago(minutes=60)),
            _make_trigger_comment("test-user", _iso_ago(minutes=30)),
        ],
        "raw_commits": [
            _make_commit(_iso_ago(minutes=10)),  # New commits since review
        ],
        "raw_pr": {"created_at": _iso_ago(minutes=180)},
        "bot_reaction_count": 0,  # Last trigger not yet acked — age guard doesn't apply
    }
    # status: should NOT be in-progress (guard not fired yet)
    status = _run_helper("status", fixture)
    assert (
        status.stdout.strip() != "in-progress"
    ), f"Should NOT block at count=2 (< MAX_RE_TRIGGERS=3), got: {status.stdout.strip()}"


# --- Tests for local trigger-timestamp file (API propagation delay guard) ---


def test_trigger_writes_local_timestamp_on_success():
    """Successful trigger → writes _TRIGGER_TS_FILE to TMPDIR.

    This is the core of the API-propagation-delay guard: if a sequential
    caller checks within TRIGGER_GRACE_SECONDS, it must see "in-progress"
    even before the GitHub API surfaces the comment.
    """
    reviewed_at = _iso_ago(minutes=60)
    fixture = {
        "pr_number": 555,
        "raw_comments": [
            _make_greptile_comment(3, reviewed_at=reviewed_at),
            _make_trigger_comment("test-user", _iso_ago(minutes=45)),
        ],
        "raw_commits": [_make_commit(_iso_ago(minutes=10))],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 1,
    }
    # Run trigger with a known TMPDIR so we can check for the TS file
    repo = "gptme/gptme"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fake_gh = tmp_path / "gh"
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
        gh_log = tmp_path / "gh-log.json"
        env = os.environ.copy()
        env.update(
            GH_FIXTURE=str(fixture_path),
            GH_LOG=str(gh_log),
            GITHUB_AUTHOR="test-user",
            PATH=f"{tmp}:{env['PATH']}",
            TMPDIR=tmp,
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), "trigger", repo, str(fixture["pr_number"])],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "Re-triggered successfully" in result.stdout

        pr_num = int(str(fixture["pr_number"]))
        ts_file = tmp_path / f"greptile-trigger-ts-{_pr_hash(repo, pr_num)}.txt"
        assert ts_file.exists(), "_TRIGGER_TS_FILE should have been written"
        ts = ts_file.read_text().strip()
        # Timestamp should be a recent ISO 8601 UTC string
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(ts.replace("Z", "+00:00"))
        ).total_seconds()
        assert 0 <= age < 30, f"Timestamp should be very recent, got age={age}s"


def test_local_timestamp_blocks_sequential_retrigger():
    """Pre-seeded local timestamp < 15min old → in-progress even with no API trigger comment.

    Simulates the INCIDENT #5 scenario: fb40 session triggers at 00:15Z, post-session
    pipeline runs at 00:20Z. The second call sees no API trigger comment (API propagation
    delay) but should still be blocked by the local timestamp file.
    """
    reviewed_at = _iso_ago(minutes=60)
    fixture = {
        "pr_number": 504,
        # No trigger comment visible in API (propagation delay simulation)
        "raw_comments": [
            _make_greptile_comment(4, reviewed_at=reviewed_at),
        ],
        "raw_commits": [_make_commit(_iso_ago(minutes=10))],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 0,
    }
    # Pre-seed: local TS file says we triggered 5 minutes ago
    pre_ts = _iso_ago(minutes=5)
    result, gh_log = _run_helper(
        "trigger",
        fixture,
        capture_gh_log=True,
        pre_trigger_ts=pre_ts,
        repo="gptme/gptme-contrib",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert (
        not gh_log
    ), f"Should NOT have posted trigger (local TS shows recent trigger), got: {gh_log}"
    assert (
        "in-flight" in result.stdout
    ), f"Expected in-flight message, got: {result.stdout}"


def test_local_timestamp_status_returns_in_progress():
    """Pre-seeded local timestamp < 15min → status returns 'in-progress'."""
    reviewed_at = _iso_ago(minutes=60)
    fixture = {
        "pr_number": 505,
        "raw_comments": [
            _make_greptile_comment(3, reviewed_at=reviewed_at),
        ],
        "raw_commits": [_make_commit(_iso_ago(minutes=10))],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 0,
    }
    pre_ts = _iso_ago(minutes=3)
    status = _run_helper(
        "status", fixture, pre_trigger_ts=pre_ts, repo="gptme/gptme-contrib"
    )
    assert (
        status.stdout.strip() == "in-progress"
    ), f"Expected in-progress (local TS < 15min), got: {status.stdout.strip()}"


def test_stale_local_timestamp_does_not_block():
    """Pre-seeded local timestamp > 15min old → NOT treated as in-progress.

    Once the grace window passes, the local TS is stale and the API is the
    authoritative source for trigger status.
    """
    reviewed_at = _iso_ago(minutes=60)
    fixture = {
        "pr_number": 555,
        "raw_comments": [
            _make_greptile_comment(4, reviewed_at=reviewed_at),
        ],
        "raw_commits": [_make_commit(_iso_ago(minutes=10))],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 0,
    }
    # Local TS is 20 minutes old → beyond TRIGGER_GRACE_SECONDS (15min = 900s)
    pre_ts = _iso_ago(minutes=20)
    status = _run_helper("status", fixture, pre_trigger_ts=pre_ts, repo="gptme/gptme")
    # Should NOT be in-progress (old TS doesn't block)
    assert (
        status.stdout.strip() != "in-progress"
    ), f"Stale local TS (20min) should not report in-progress, got: {status.stdout.strip()}"


def test_local_timestamp_from_previous_cycle_does_not_block():
    """Local TS from before the last Greptile review is from a stale cycle → doesn't block.

    After Greptile posts a new review, an old local TS (from a trigger that led to
    that review) should not block re-review triggers for new commits.
    """
    # Greptile reviewed 30 minutes ago
    reviewed_at = _iso_ago(minutes=30)
    fixture = {
        "pr_number": 777,
        "raw_comments": [
            _make_greptile_comment(4, reviewed_at=reviewed_at),
        ],
        "raw_commits": [_make_commit(_iso_ago(minutes=10))],
        "raw_pr": {"created_at": _iso_ago(minutes=120)},
        "bot_reaction_count": 0,
    }
    # Local TS from 35 minutes ago (BEFORE the last review — stale cycle)
    pre_ts = _iso_ago(minutes=35)
    status = _run_helper("status", fixture, pre_trigger_ts=pre_ts, repo="gptme/gptme")
    # Should NOT be in-progress — the stale cycle TS is before the review
    assert (
        status.stdout.strip() != "in-progress"
    ), f"Pre-review local TS should not block re-review, got: {status.stdout.strip()}"
