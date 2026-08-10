"""Tests for the `@TimeToBuildBob fix` maintainer trigger in activity-gate.sh.

`@TimeToBuildBob review` forces a fresh review; `@TimeToBuildBob fix` forces a
worker to act on the PR's outstanding review findings, by emitting the
`greptile_needs_fix` item that Project Monitoring already routes to the fix lane.

The load-bearing property is SINGLE EMIT PER TRIGGER COMMENT. There is a recorded
incident (memory/feedback_greptile_spam_bug.md) where a poll waiting on a reaction
that could never appear posted `@greptileai review` 29 times on one PR, so
`test_single_trigger_emits_exactly_once_across_runs` replays the reaction the
first run posted into the second run's fixture and asserts the second run is
silent — asserted on emitted items, not on internal state.

Convention follows tests/test_greptile_helper.py: a fake `gh` stub driven by a
JSON fixture, written to a temp dir and prepended to PATH.
"""

from __future__ import annotations

import json
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
BOT = "TimeToBuildBob"
COMMENT_ID = 987654321

FAKE_GH = r'''#!/usr/bin/env python3
"""Fake gh CLI for activity-gate.sh fix-request tests.

Serves the PR list (with comments) and the issue-comment reactions endpoint from
a JSON fixture, applies --jq via the real jq binary, and appends every reaction
POST to GH_LOG as JSONL so the test can replay it into a later fixture.
"""
from pathlib import Path
import json
import os
import subprocess as sp
import sys

fixture = json.loads(Path(os.environ["GH_FIXTURE"]).read_text())
argv = sys.argv[1:]

if not argv:
    sys.exit(2)

if argv[0] == "pr" and argv[1:2] == ["list"]:
    print(json.dumps(fixture["prs"]))
    sys.exit(0)

if argv[0] in ("repo", "issue", "run") and argv[1:2] == ["list"]:
    print("[]")
    sys.exit(0)

if argv[0] != "api":
    sys.exit(0)

# Parse the api invocation
endpoint = ""
jq_expr = ""
method = "GET"
fields = {}
i = 1
while i < len(argv):
    a = argv[i]
    if a == "--jq":
        jq_expr = argv[i + 1]; i += 2; continue
    if a == "-X":
        method = argv[i + 1]; i += 2; continue
    if a in ("-f", "-F") and i + 1 < len(argv):
        kv = argv[i + 1]
        if "=" in kv:
            k, v = kv.split("=", 1)
            fields[k] = v
        i += 2; continue
    if a in ("-q", "-H"):
        i += 2; continue
    if a == "--paginate":
        i += 1; continue
    if a.startswith("-"):
        i += 1; continue
    endpoint = a; i += 1

if endpoint == "notifications":
    sys.exit(0)

if endpoint.endswith("/reactions"):
    if fixture.get("reactions_api_error"):
        sys.exit(1)
    if method == "POST":
        if fixture.get("reaction_post_error"):
            sys.exit(1)
        with open(os.environ["GH_LOG"], "a") as fh:
            fh.write(json.dumps({"endpoint": endpoint, "content": fields.get("content")}) + "\n")
        print("{}")
        sys.exit(0)
    data = fixture.get("reactions", [])
else:
    # Every other REST call the gate makes (greptile comment sweep, merge
    # permission probes, ...) sees an empty result.
    if jq_expr:
        sys.exit(0)
    print("[]")
    sys.exit(0)

raw = json.dumps(data)
if jq_expr:
    r = sp.run(["jq", "-r", jq_expr], input=raw, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
else:
    print(raw)
'''


def _comment(
    *,
    body: str,
    author: str = "ErikBjare",
    association: str = "OWNER",
    comment_id: int = COMMENT_ID,
    created_at: str = "2026-08-10T12:00:00Z",
) -> dict:
    return {
        "author": {"login": author},
        "authorAssociation": association,
        "body": body,
        "createdAt": created_at,
        "id": "IC_kwDOfake",
        "includesCreatedEdit": False,
        "isMinimized": False,
        "minimizedReason": "",
        "reactionGroups": [],
        "url": f"https://github.com/{TEST_REPO}/pull/{TEST_PR}#issuecomment-{comment_id}",
        "viewerDidAuthor": author == BOT,
    }


def _pr(comments: list[dict]) -> dict:
    return {
        "number": TEST_PR,
        "title": f"Test PR #{TEST_PR}",
        "updatedAt": "2026-08-10T12:00:00Z",
        "comments": comments,
        "latestReviews": [],
        "statusCheckRollup": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "headRefOid": TEST_HEAD_SHA,
        "isDraft": False,
    }


def _run_gate(
    tmp: Path, fixture: dict, *, state_dir: Path, gh_log: Path
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """Run activity-gate.sh once. Returns (result, reaction POSTs made)."""
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))

    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["GH_FIXTURE"] = str(fixture_path)
    env["GH_LOG"] = str(gh_log)
    env["PATH"] = f"{tmp}:{env['PATH']}"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--author",
            BOT,
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
        timeout=120,
    )
    posts = [
        json.loads(line)
        for line in (gh_log.read_text().splitlines() if gh_log.exists() else [])
        if line.strip()
    ]
    return result, posts


def _fix_items(result: subprocess.CompletedProcess[str]) -> list[dict]:
    """Emitted items produced by the fix-request path, identified by its token."""
    items = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "fix_request" in (obj.get("detail") or ""):
            items.append(obj)
    return items


def _fixture(comments: list[dict], **extra: object) -> dict:
    return {"prs": [_pr(comments)], "reactions": [], **extra}


def _trusted_request() -> list[dict]:
    return [_comment(body=f"@{BOT} fix")]


def test_trusted_request_emits_one_fix_item() -> None:
    """A well-formed maintainer request emits exactly one greptile_needs_fix."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(_trusted_request()),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert result.returncode in (0, 1), result.stderr

        items = _fix_items(result)
        assert len(items) == 1, f"expected one fix item, got {items}\n{result.stdout}"
        assert items[0]["type"] == "greptile_needs_fix", items[0]
        assert items[0]["number"] == TEST_PR
        assert "[maintainer fix request]" in items[0]["title"], items[0]

        # The 👀 reaction is the watermark and must have been posted.
        assert len(posts) == 1, posts
        assert posts[0]["content"] == "eyes", posts[0]
        assert str(COMMENT_ID) in posts[0]["endpoint"], posts[0]


def test_single_trigger_emits_exactly_once_across_runs() -> None:
    """THE anti-spam test: one trigger comment → one emit, forever.

    Run 1 emits and posts 👀. That reaction is replayed into run 2's fixture
    exactly as GitHub would serve it back. Run 2 must emit nothing.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        gh_log = tmp / "gh.log"

        first, posts = _run_gate(
            tmp, _fixture(_trusted_request()), state_dir=state_dir, gh_log=gh_log
        )
        assert len(_fix_items(first)) == 1, first.stdout
        assert posts, "run 1 must have posted the watermark reaction"

        # Feed run 1's reaction POST back in as GitHub would now report it.
        replayed = [
            {"id": 1, "content": p["content"], "user": {"login": BOT}} for p in posts
        ]
        gh_log.unlink()
        second, posts2 = _run_gate(
            tmp,
            _fixture(_trusted_request(), reactions=replayed),
            state_dir=state_dir,
            gh_log=gh_log,
        )
        assert _fix_items(second) == [], (
            "a served trigger comment re-emitted — this is the 29x greptile spam "
            f"shape. stdout: {second.stdout}"
        )
        assert (
            posts2 == []
        ), f"no reaction should be posted on a served trigger: {posts2}"


def test_existing_eyes_reaction_suppresses_emit() -> None:
    """A trigger already carrying our 👀 is served, whatever wrote it."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(
                _trusted_request(),
                reactions=[{"id": 1, "content": "eyes", "user": {"login": BOT}}],
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_reaction_from_someone_else_does_not_count_as_served() -> None:
    """A human's 👀 is not our watermark — the request is still pending."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, _ = _run_gate(
            tmp,
            _fixture(
                _trusted_request(),
                reactions=[{"id": 1, "content": "eyes", "user": {"login": "someone"}}],
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert len(_fix_items(result)) == 1, result.stdout


def test_untrusted_association_does_not_emit() -> None:
    """Anyone can comment on a public PR; only maintainers spend worker budget."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(
                [_comment(body=f"@{BOT} fix", author="drive-by", association="NONE")]
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_self_authored_trigger_does_not_emit() -> None:
    """Self-trigger guard: our own comments quote the phrase in the review footer."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(
                [
                    _comment(
                        body=f"Re-request with `@{BOT} review`.\n@{BOT} fix",
                        author=BOT,
                        association="MEMBER",
                    )
                ]
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_reactions_api_failure_fails_toward_skip() -> None:
    """A guard that fails open is how the 29x spam happened. Fail closed."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(_trusted_request(), reactions_api_error=True),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_failed_reaction_post_suppresses_emit() -> None:
    """No watermark, no emit — otherwise the next cycle emits again."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(_trusted_request(), reaction_post_error=True),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_inline_mention_does_not_trigger() -> None:
    """Whole-line only: prose about the trigger must not fire it."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, posts = _run_gate(
            tmp,
            _fixture(
                [_comment(body=f"could you ask @{BOT} fix the lint error please")]
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert _fix_items(result) == [], result.stdout
        assert posts == [], posts


def test_trigger_on_its_own_line_in_a_longer_comment_fires() -> None:
    """CRLF bodies and surrounding prose lines must not defeat the anchor."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result, _ = _run_gate(
            tmp,
            _fixture(
                [_comment(body=f"P1 looks real to me.\r\n  @{BOT}   FIX  \r\nthanks")]
            ),
            state_dir=state_dir,
            gh_log=tmp / "gh.log",
        )
        assert len(_fix_items(result)) == 1, result.stdout
