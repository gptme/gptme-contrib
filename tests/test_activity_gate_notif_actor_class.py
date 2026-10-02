"""Author/comment PR notifications whose trigger is bot-only must not emit.

The 2026-09-27 PM dispatch audit attributes 47% of equivalent dispatches (31.7
slot-hours, 2.33M output tokens) to the undifferentiated ``notification``
class. That class merges human asks (``mention``/``assign``/
``review_requested``) with ``author``/``comment`` bumps whose only new activity
is automation (Codecov/Greptile/Dependabot). This gate preserves the human
reasons and suppresses an ``author``/``comment`` PR notification only when the
most recent comment/review actor is a bot and no later human activity exists.

The emitted ``detail`` carries the actor class (``; actor_class=<class>``) so
the reason and actor survive into the grouped work file and the dispatch
ledger for a by-subtype seven-day readout.
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

NOTIF_ID = "25314090843"
NOTIF_REPO = "ActivityWatch/aw-server-rust"
NOTIF_NUMBER = 660

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess as sp, sys

argv = sys.argv[1:]
notif_updated = os.environ.get("TEST_NOTIF_UPDATED_AT", "2026-08-26T17:15:04Z")
notif_id = os.environ.get("TEST_NOTIF_ID", "25314090843")
notif_repo = os.environ.get("TEST_NOTIF_REPO", "ActivityWatch/aw-server-rust")
notif_number = int(os.environ.get("TEST_NOTIF_NUMBER", "660"))
notif_reason = os.environ.get("TEST_NOTIF_REASON", "author")
subject_type = os.environ.get("TEST_SUBJECT_TYPE", "PullRequest")
subject_state = os.environ.get("TEST_SUBJECT_STATE", "open")
head_sha = os.environ.get("TEST_HEAD_SHA", "a65ead926a4080f6a17de9af384a6a87774f5761")
comments = json.loads(os.environ.get("TEST_COMMENTS_JSON", "[]"))
reviews = json.loads(os.environ.get("TEST_REVIEWS_JSON", "[]"))


def apply_jq(data, jq_expr):
    if not jq_expr:
        return json.dumps(data)
    r = sp.run(["jq", "-r", jq_expr], input=json.dumps(data), capture_output=True, text=True)
    return r.stdout.strip()


def parse_endpoint_and_jq(args):
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


def pages(items):
    return [items[index:index + 100] for index in range(0, len(items), 100)] or [[]]


if not argv:
    sys.exit(2)

if argv[0] == "api":
    endpoint, jq_expr = parse_endpoint_and_jq(argv)
    if "notifications" in endpoint:
        notifs = [{
            "id": notif_id,
            "reason": notif_reason,
            "updated_at": notif_updated,
            "subject": {
                "title": "build(deps): bump aw-webui",
                "url": f"https://api.github.com/repos/{notif_repo}/pulls/{notif_number}",
                "type": subject_type,
            },
            "repository": {"full_name": notif_repo},
        }]
        print(apply_jq(notifs, jq_expr))
        sys.exit(0)
    if "/issues/" in endpoint and "/comments" in endpoint:
        if "--paginate" in argv:
            out = pages(comments)
        else:
            out = comments[:100]
        print(apply_jq(out, jq_expr) if jq_expr else json.dumps(out))
        sys.exit(0)
    if "/pulls/" in endpoint and "/reviews" in endpoint:
        if "--paginate" in argv:
            out = pages(reviews)
        else:
            out = reviews[:100]
        print(apply_jq(out, jq_expr) if jq_expr else json.dumps(out))
        sys.exit(0)
    if endpoint.endswith(f"/issues/{notif_number}"):
        print(apply_jq({"state": subject_state}, jq_expr))
        sys.exit(0)
    if "/pulls/" in endpoint and endpoint.endswith(str(notif_number)):
        print(apply_jq({"head": {"sha": head_sha}}, jq_expr))
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    *,
    reason: str = "author",
    comments: list[dict] | None = None,
    reviews: list[dict] | None = None,
    author: str = "test-author",
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_NOTIF_ID"] = NOTIF_ID
    env["TEST_NOTIF_REPO"] = NOTIF_REPO
    env["TEST_NOTIF_NUMBER"] = str(NOTIF_NUMBER)
    env["TEST_NOTIF_REASON"] = reason
    env["TEST_COMMENTS_JSON"] = json.dumps(comments or [])
    env["TEST_REVIEWS_JSON"] = json.dumps(reviews or [])
    env.pop("BOT_USERNAME", None)
    env["PATH"] = f"{tmp}:{env['PATH']}"

    # Established state dir: seed a sibling so first-sight emits.
    (state_dir / "notif-99999999999.state").write_text("2026-08-01T00:00:00Z")

    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            author,
            "--org",
            "ActivityWatch",
            "--repo",
            NOTIF_REPO,
            "--state-dir",
            str(state_dir),
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _emitted_notifications(stdout: str) -> list[dict]:
    items = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "notification":
            items.append(obj)
    return items


def _bot_comment(created_at: str = "2026-08-26T16:30:00Z") -> dict:
    return {
        "user": {"login": "codecov[bot]", "type": "Bot"},
        "body": "Coverage report",
        "created_at": created_at,
    }


def _human_comment(created_at: str = "2026-08-26T17:00:00Z") -> dict:
    return {
        "user": {"login": "ErikBjare", "type": "User"},
        "body": "Please update the changelog.",
        "created_at": created_at,
    }


def test_bot_only_author_notification_suppressed() -> None:
    """A Codecov-only bump on an author notification is consumed, not dispatched."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, comments=[_bot_comment()])
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout
        # Persisted so the bump is not retried until updated_at advances.
        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert state_file.exists()
        assert state_file.read_text().strip() == "2026-08-26T17:15:04Z"


def test_bot_only_comment_notification_suppressed() -> None:
    """Same suppression applies to the `comment` reason on a PR subject."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, reason="comment", comments=[_bot_comment()])
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout


def test_human_reply_after_bot_activity_emits_with_actor_class() -> None:
    """A later human comment reopens the thread and the actor class is carried."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, comments=[_bot_comment(), _human_comment()])
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author; actor_class=human"


def test_human_review_after_bot_activity_emits() -> None:
    """A human review (not just a comment) is later human activity."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            comments=[_bot_comment()],
            reviews=[
                {
                    "user": {"login": "ErikBjare", "type": "User"},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-08-26T17:00:00Z",
                }
            ],
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author; actor_class=human"


def test_direct_mention_not_suppressed_by_bot_activity() -> None:
    """mention stays emit-eligible and keeps its bare token (routing contract)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, reason="mention", comments=[_bot_comment()])
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "mention"


def test_self_comment_does_not_count_as_bot_activity() -> None:
    """The running identity's own comment is not a bot bump (no over-suppression).

    The generic bot-only filter must not swallow a notification merely because
    the latest actor is the agent itself; self-chatter is handled by the
    existing maintainer-waiting / self-trigger guards, not this one.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            comments=[
                {
                    "user": {"login": "test-author", "type": "User"},
                    "body": "Pushed a follow-up.",
                    "created_at": "2026-08-26T17:00:00Z",
                }
            ],
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author; actor_class=self"


def test_actor_probe_failure_fails_open() -> None:
    """An empty/no-activity fixture is 'unknown' and must still emit."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, comments=[], reviews=[])
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author; actor_class=unknown"
