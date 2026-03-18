"""Black-box tests for scripts/github/greptile-helper.sh.

The fake gh stub returns raw GitHub API response arrays and applies --jq
via real jq, so the script's jq expressions (score regex, date filtering,
sort ordering) are exercised by every test.
"""

from __future__ import annotations

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
i = 1
while i < len(argv):
    arg = argv[i]
    if arg == "--jq":
        jq_expr = argv[i + 1]; i += 2; continue
    if arg in ("-q", "-H", "-f", "-F"):
        i += 2; continue
    if arg == "--paginate":
        i += 1; continue
    if arg.startswith("-"):
        i += 1; continue
    endpoint = arg; i += 1

github_author = os.environ.get("GITHUB_AUTHOR", "test-user")
pr_number = fixture["pr_number"]

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
    r = sp.run(["jq", jq_expr], input=raw, capture_output=True, text=True)
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


@overload
def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: Literal[True],
) -> tuple[subprocess.CompletedProcess[str], str]: ...


@overload
def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: Literal[False] = ...,
) -> subprocess.CompletedProcess[str]: ...


def _run_helper(
    command: str,
    fixture: dict[str, object],
    *,
    capture_gh_log: bool = False,
) -> subprocess.CompletedProcess[str] | tuple[subprocess.CompletedProcess[str], str]:
    """Run greptile-helper.sh with a fake gh stub.

    When capture_gh_log=True, returns (result, gh_log_content) tuple.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))

        fake_gh = tmp_path / "gh"
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        gh_log = tmp_path / "gh-log.json"
        env = os.environ.copy()
        env["GH_FIXTURE"] = str(fixture_path)
        env["GH_LOG"] = str(gh_log)
        env["GITHUB_AUTHOR"] = "test-user"
        env["PATH"] = f"{tmp}:{env['PATH']}"

        result = subprocess.run(
            ["bash", str(SCRIPT), command, "gptme/gptme", str(fixture["pr_number"])],
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
    """Trigger > 20min with bot ack → stale."""
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
    assert result.returncode == 0, f"stderr: {result.stderr}"

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "stale"


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


def test_old_pr_without_review_is_actionable():
    """Old PR (> 20 min, no review, no trigger) → none (safe to trigger)."""
    fixture = {
        "pr_number": 789,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=60)},
        "bot_reaction_count": 0,
    }
    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "none"

    result = _run_helper("check", fixture)
    assert result.returncode == 0, f"Should be actionable. stderr: {result.stderr}"


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


def test_trigger_posts_comment_on_old_unreviewed_pr():
    """Trigger on old PR with no review → posts @greptileai review comment."""
    fixture = {
        "pr_number": 999,
        "raw_comments": [],
        "raw_commits": [],
        "raw_pr": {"created_at": _iso_ago(minutes=60)},
        "bot_reaction_count": 0,
    }
    result, gh_log = _run_helper("trigger", fixture, capture_gh_log=True)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Triggered successfully" in result.stdout
    assert gh_log, "gh pr comment was never called"
    assert "@greptileai review" in gh_log


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
    assert "Waiting for auto-review" in result.stdout
