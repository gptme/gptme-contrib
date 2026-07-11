"""Tests for human-review priority tokens in activity-gate.sh (§7b human > bot).

The gate must mark pr_update items whose triggering activity is from a human
(non-bot, non-AUTHOR) so the dispatcher can sort them ahead of the bot backlog
and grant bounded cap overflow. Regression context: Erik's CHANGES_REQUESTED
review on gptme/gptme#3178 (2026-07-11) was emitted every cycle but skipped at
the global slot cap behind a 3-day bot-priority backlog.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

TEST_REPO = "testorg/testrepo"
TEST_PR = 42

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys

argv = sys.argv[1:]
scenario = os.environ.get("TEST_SCENARIO", "human_cr")

if not argv:
    sys.exit(2)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    sys.exit(0)

if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
    comments = []
    latest_reviews = []
    if scenario == "human_cr":
        # Bot review first, then a human CHANGES_REQUESTED (the #3178 shape).
        latest_reviews = [
            {
                "author": {"login": "greptile-apps"},
                "state": "COMMENTED",
                "submittedAt": "2026-07-11T12:00:00Z",
                "body": "",
            },
            {
                "author": {"login": "ErikBjare"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-07-11T13:26:04Z",
                "body": "please fix",
            },
        ]
    elif scenario == "bot_last":
        latest_reviews = [
            {
                "author": {"login": "greptile-apps"},
                "state": "COMMENTED",
                "submittedAt": "2026-07-11T13:00:00Z",
                "body": "bot feedback",
            },
        ]
    elif scenario == "human_comment":
        comments = [
            {
                "author": {"login": "ErikBjare"},
                "createdAt": "2026-07-11T13:00:00Z",
                "body": "looks close, one question",
            },
        ]
    pr = [{
        "number": 42,
        "title": "Test PR",
        "updatedAt": "2026-07-11T13:30:00Z",
        "comments": comments,
        "latestReviews": latest_reviews,
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "deadbeef1234",
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

if argv[0] == "api":
    if "--jq" in argv:
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(tmp: Path, scenario: str) -> list[dict]:
    """Run the gate against fake gh; return emitted jsonl pr_update items."""
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    state_dir = tmp / f"state-{scenario}"
    state_dir.mkdir()
    # Seed PR state older than updatedAt so the update is reported (a fresh
    # state dir only seeds without emitting).
    repo_safe = TEST_REPO.replace("/", "-")
    (state_dir / f"{repo_safe}-pr-{TEST_PR}.state").write_text("2026-07-11T00:00:00Z")

    env = os.environ.copy()
    env["TEST_SCENARIO"] = scenario
    env["PATH"] = f"{tmp}:{env['PATH']}"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
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
    items = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if item.get("type") == "pr_update":
            items.append(item)
    return items


def test_human_changes_requested_emits_both_priority_tokens() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        items = _run_gate(Path(tmp_str), "human_cr")
        assert len(items) == 1, items
        detail = items[0]["detail"]
        assert "human_changes_requested" in detail, detail
        assert "human_activity" in detail, detail


def test_bot_activity_emits_no_priority_tokens() -> None:
    """Bot reviews still emit pr_update (actionable) but without priority."""
    with tempfile.TemporaryDirectory() as tmp_str:
        items = _run_gate(Path(tmp_str), "bot_last")
        assert len(items) == 1, items
        detail = items[0]["detail"]
        assert "human_changes_requested" not in detail, detail
        assert "human_activity" not in detail, detail


def test_plain_human_comment_emits_activity_token_only() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        items = _run_gate(Path(tmp_str), "human_comment")
        assert len(items) == 1, items
        detail = items[0]["detail"]
        assert "human_activity" in detail, detail
        assert "human_changes_requested" not in detail, detail
