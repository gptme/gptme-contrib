"""Black-box tests for scripts/github/greptile-helper.sh."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "greptile-helper.sh"

# The fake gh stub reads GITHUB_AUTHOR from the environment (same as the real script).
FAKE_GH = """#!/usr/bin/env python3
from pathlib import Path
import json
import os
import sys


def _endpoint(argv: list[str]) -> str:
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"--jq", "-q", "-H", "-f", "-F"}:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return ""


fixture = json.loads(Path(os.environ["GH_FIXTURE"]).read_text())
argv = sys.argv[1:]

if argv[:2] == ["pr", "comment"]:
    Path(os.environ["GH_LOG"]).write_text(json.dumps(argv))
    raise SystemExit(0)

if argv[:1] == ["api"] and len(argv) >= 2 and argv[1] == "user":
    # Return the test author for `gh api user --jq .login`
    print(os.environ.get("GITHUB_AUTHOR", "test-user"))
    raise SystemExit(0)

if not argv or argv[0] != "api":
    raise SystemExit(2)

endpoint = _endpoint(argv)
jq = ""
if "--jq" in argv:
    jq = argv[argv.index("--jq") + 1]

github_author = os.environ.get("GITHUB_AUTHOR", "test-user")

if endpoint.endswith(f"/issues/{fixture['pr_number']}/comments"):
    if f'select(.user.login == "{github_author}"' in jq:
        trigger = fixture.get("trigger_comment_info")
        print("{}" if trigger is None else json.dumps(trigger))
        raise SystemExit(0)
    if 'select(.user.login | test("greptile"; "i"))' in jq:
        print(json.dumps(fixture["review_info"]))
        raise SystemExit(0)
    raise SystemExit(3)

if endpoint.endswith(f"/pulls/{fixture['pr_number']}/commits"):
    print(str(fixture.get("new_commits", 0)))
    raise SystemExit(0)

if endpoint.endswith("/reactions"):
    if fixture.get("reactions_error"):
        raise SystemExit(1)
    print(str(fixture.get("bot_reaction_count", 0)))
    raise SystemExit(0)

raise SystemExit(4)
"""


def _iso_ago(*, minutes: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_helper(
    command: str, fixture: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))

        fake_gh = tmp_path / "gh"
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env["GH_FIXTURE"] = str(fixture_path)
        env["GH_LOG"] = str(tmp_path / "gh-log.json")
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["GITHUB_AUTHOR"] = "test-user"

        return subprocess.run(
            ["bash", str(SCRIPT), command, "gptme/gptme", str(fixture["pr_number"])],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )


def test_check_blocks_recently_acknowledged_trigger():
    fixture = {
        "pr_number": 123,
        "review_info": {"has_review": False, "score": None, "reviewed_at": None},
        "trigger_comment_info": {"id": 1, "created_at": _iso_ago(minutes=16)},
        "bot_reaction_count": 1,
        "new_commits": 0,
    }

    result = _run_helper("check", fixture)
    assert result.returncode == 1

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "in-progress"


def test_check_retries_acknowledged_trigger_after_timeout():
    fixture = {
        "pr_number": 123,
        "review_info": {"has_review": False, "score": None, "reviewed_at": None},
        "trigger_comment_info": {"id": 1, "created_at": _iso_ago(minutes=25)},
        "bot_reaction_count": 1,
        "new_commits": 0,
    }

    result = _run_helper("check", fixture)
    assert result.returncode == 0

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "stale"


def test_rereview_ignores_old_trigger_from_previous_review_cycle():
    fixture = {
        "pr_number": 123,
        "review_info": {
            "has_review": True,
            "score": 4,
            "reviewed_at": _iso_ago(minutes=10),
        },
        "trigger_comment_info": {"id": 1, "created_at": _iso_ago(minutes=30)},
        "bot_reaction_count": 1,
        "new_commits": 1,
    }

    result = _run_helper("check", fixture)
    assert result.returncode == 0

    status = _run_helper("status", fixture)
    assert status.stdout.strip() == "needs-re-review"
