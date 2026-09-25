"""The merge-ready suppression must honor $BOT_USERNAME, not Bob's login.

gptme/gptme-contrib#1705: a hard-coded ``user.login == "TimeToBuildBob"`` jq
filter meant other agents never recognized their own maintainer-waiting
comments, so merge-ready PRs kept re-notifying.

These tests run the real ``_is_permission_blocked_merge_ready_pr_uncached`` body
against a stubbed ``gh``. A source-text assertion would not catch the filter
being built by string interpolation, which breaks (and is an injection vector)
as soon as an env-supplied login contains a quote or backslash.

Convention follows tests/test_activity_gate_fix_request.py: a fake `gh` stub
driven by a JSON fixture, written to a temp dir and prepended to PATH.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "check-notifications.sh"

TEST_REPO = "testorg/testrepo"
TEST_NUMBER = 42
DEFAULT_BOT = "TimeToBuildBob"
OTHER_BOT = "TimeToLearnAlice"
WAITING_PHRASE = "waiting only on a maintainer click"

FAKE_GH = r'''#!/usr/bin/env python3
"""Fake gh CLI for check-notifications.sh merge-ready suppression tests.

Applies ``--jq`` with the real jq binary, like gh does, and exits non-zero when
the jq program is invalid. That matters: the pre-fix code built the filter by
string interpolation, so an env-supplied login containing a quote produced an
invalid jq program — a failure the stub must reproduce to guard against it.
"""
from pathlib import Path
import json
import os
import subprocess as sp
import sys

fixture = json.loads(Path(os.environ["GH_FIXTURE"]).read_text())
argv = sys.argv[1:]

if argv[:2] == ["pr", "view"]:
    print(json.dumps(fixture["pr"]))
    sys.exit(0)

if argv[:1] != ["api"]:
    sys.exit(0)

endpoint = ""
jq_expr = ""
i = 1
while i < len(argv):
    a = argv[i]
    if a == "--jq":
        jq_expr = argv[i + 1]; i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    endpoint = a; i += 1

if "/comments" not in endpoint:
    print("[]")
    sys.exit(0)

raw = json.dumps(fixture["comments"])
if not jq_expr:
    print(raw)
    sys.exit(0)

r = sp.run(["jq", "-r", jq_expr], input=raw, capture_output=True, text=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr)
    sys.exit(1)
sys.stdout.write(r.stdout)
'''

#: Sources the function bodies from the script under test rather than copying
#: them, so the test tracks the implementation. ``awk`` extracts each function
#: by its definition line up to the next column-0 ``}``.
#:
#: That pattern assumes the script keeps column-0 closing braces (it does, repo-
#: wide). If a reformat ever breaks the assumption the extraction would quietly
#: define nothing and the assertion below would silently test nothing, so each
#: extraction is checked and the driver exits 99 — a code no test expects.
DRIVER = r"""
SCRIPT=$1
repo=$2
number=$3
for fn in _is_permission_blocked_merge_ready_pr_uncached is_permission_blocked_merge_ready_pr; do
    body=$(awk "/^${fn}\\(\\)/,/^}/" "$SCRIPT")
    [ -n "$body" ] || { echo "extraction empty for $fn" >&2; exit 99; }
    eval "$body" || { echo "extraction unparsable for $fn" >&2; exit 99; }
    type -t "$fn" >/dev/null || { echo "extraction defined no $fn" >&2; exit 99; }
done
is_permission_blocked_merge_ready_pr "$repo" "$number"
"""


def _write_fake_gh(tmp: Path) -> None:
    gh = tmp / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)


def _fixture(comments: list[dict], **overrides: object) -> dict:
    fixture: dict = {
        "pr": {
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        "comments": comments,
    }
    fixture.update(overrides)
    return fixture


def _comment(login: str, body: str = WAITING_PHRASE) -> dict:
    return {"user": {"login": login}, "body": body}


def _suppressed(
    fixture: dict,
    *,
    bot_username: str | None,
) -> int:
    """Run the real suppression check. Returns its exit code (0 = suppressed)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _write_fake_gh(tmp)
        fixture_path = tmp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        driver = tmp / "driver.sh"
        driver.write_text(DRIVER)

        env = dict(os.environ)
        env["GH_FIXTURE"] = str(fixture_path)
        env["PATH"] = f"{tmp}:{env['PATH']}"
        # Force the uncached path so the assertions read the real filter.
        env["BOB_NOTIFICATION_CACHE_DISABLE"] = "1"
        if bot_username is None:
            env.pop("BOT_USERNAME", None)
        else:
            env["BOT_USERNAME"] = bot_username

        result = subprocess.run(
            ["bash", str(driver), str(SCRIPT), TEST_REPO, str(TEST_NUMBER)],
            env=env,
            capture_output=True,
            text=True,
        )
        return result.returncode


def test_another_agents_waiting_comment_suppresses_that_agent() -> None:
    """A non-Bob agent must recognize its own maintainer-waiting comment."""
    assert _suppressed(_fixture([_comment(OTHER_BOT)]), bot_username=OTHER_BOT) == 0


def test_another_agent_does_not_see_the_default_logins_comment() -> None:
    """Bob's waiting comment must not suppress Alice's notifications."""
    assert _suppressed(_fixture([_comment(DEFAULT_BOT)]), bot_username=OTHER_BOT) == 1


def test_default_login_matches_when_bot_username_is_unset() -> None:
    """Backwards compatibility: no $BOT_USERNAME still means Bob."""
    assert _suppressed(_fixture([_comment(DEFAULT_BOT)]), bot_username=None) == 0


def test_login_with_jq_special_characters_does_not_break_the_filter() -> None:
    """A login containing ``"`` must not turn into a jq syntax error.

    With string interpolation the filter becomes invalid jq, the command fails,
    and the function returns 2 (API/permission failure) instead of 1. Passing
    the login via ``jq --arg`` keeps the program valid, so the correct answer is
    a plain "no matching comment" (1).
    """
    assert (
        _suppressed(_fixture([_comment(DEFAULT_BOT)]), bot_username='Bad"Name\\z') == 1
    )


def test_special_character_login_still_suppresses_its_own_comment() -> None:
    """The same odd login must still match a comment it authored."""
    assert (
        _suppressed(_fixture([_comment('Bad"Name\\z')]), bot_username='Bad"Name\\z')
        == 0
    )
