"""Author-reason PR notifications on a maintainer-waiting PR must not emit.

Codecov/Greptile comments re-unreads the author's notification thread. The
merge_ready path already suppresses those PRs once a waiting comment exists,
but check_notifications still emitted them as type=notification, which
dispatched NOOP sessions until retry-budget exhaustion
(ActivityWatch/aw-server-rust#660).

mention/assign/review_requested stay emit-eligible — those are human asks.
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
waiting_comment = os.environ.get("TEST_WAITING_COMMENT", "1")
# Human comment after the bot's waiting comment: the human's message is now latest.
human_after_waiting = os.environ.get("TEST_HUMAN_AFTER_WAITING", "0")
comment_count = int(os.environ.get("TEST_COMMENT_COUNT", "1"))


def apply_jq(data, jq_expr):
    if not jq_expr:
        return json.dumps(data)
    r = sp.run(
        ["jq", "-r", jq_expr],
        input=json.dumps(data),
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def parse_endpoint_and_jq(args):
    endpoint, jq_expr = "", ""
    i = 1
    while i < len(args):
        if args[i] == "--jq" and i + 1 < len(args):
            jq_expr = args[i + 1]
            i += 2
            continue
        if args[i] in ("-f", "-F", "-H", "-q") and i + 1 < len(args):
            i += 2
            continue
        if args[i] in ("--paginate", "--silent"):
            i += 1
            continue
        if args[i].startswith("--"):
            i += 1
            continue
        if args[i] != "api":
            endpoint = args[i]
        i += 1
    return endpoint, jq_expr


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
    if "comments" in endpoint:
        if waiting_comment == "1":
            comments = [{
                "user": {"login": "TimeToBuildBob"},
                "body": "CI-green and mergeable — waiting only on a maintainer click.",
            }]
            comments.extend(
                {
                    "user": {"login": "codecov[bot]"},
                    "body": f"Coverage report {index}",
                }
                for index in range(1, comment_count)
            )
            if human_after_waiting == "1":
                # A maintainer replied after the bot's waiting comment, e.g. asking
                # for a docs update.  The human's comment is now the latest — the
                # notification must NOT be suppressed.
                comments.append({
                    "user": {"login": "ErikBjare"},
                    "body": "Please also update the changelog before merging.",
                })
        else:
            comments = []
        if "--paginate" in argv:
            pages = [comments[index:index + 100] for index in range(0, len(comments), 100)]
            print(apply_jq(pages, jq_expr) if jq_expr else json.dumps(pages))
        else:
            page = comments[:100]
            print(apply_jq(page, jq_expr) if jq_expr else json.dumps(page))
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
    subject_type: str = "PullRequest",
    waiting: str = "1",
    human_after_waiting: str = "0",
    comment_count: int = 1,
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_NOTIF_ID"] = NOTIF_ID
    env["TEST_NOTIF_REPO"] = NOTIF_REPO
    env["TEST_NOTIF_NUMBER"] = str(NOTIF_NUMBER)
    env["TEST_NOTIF_REASON"] = reason
    env["TEST_SUBJECT_TYPE"] = subject_type
    env["TEST_WAITING_COMMENT"] = waiting
    env["TEST_HUMAN_AFTER_WAITING"] = human_after_waiting
    env["TEST_COMMENT_COUNT"] = str(comment_count)
    env["PATH"] = f"{tmp}:{env['PATH']}"

    # Established state dir: seed a sibling so first-sight emits.
    (state_dir / "notif-99999999999.state").write_text("2026-08-01T00:00:00Z")

    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
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


def test_author_pr_notification_suppressed_when_waiting_comment_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir)
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout
        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert state_file.exists()
        assert state_file.read_text().strip() == "2026-08-26T17:15:04Z"


def test_mention_still_emits_on_waiting_pr() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, reason="mention")
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "mention"
        assert "subject_type" not in emitted[0]


def test_author_pr_emits_without_waiting_comment() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, waiting="0")
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"


def test_author_pr_emits_when_human_commented_after_waiting() -> None:
    """Regression: a maintainer change-request after the bot's waiting comment.

    When a human replies to a bot-authored PR *after* the bot has posted its
    "waiting only on a maintainer click" comment, the 'author' notification
    must reach the dispatcher.  The previous broad suppression (any waiting
    comment ⇒ suppress) would have silently discarded the human's request.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, waiting="1", human_after_waiting="1")
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"


def test_author_pr_emits_when_human_comment_is_after_page_one() -> None:
    """The newest comment must be selected across all paginated results."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            waiting="1",
            human_after_waiting="1",
            comment_count=100,
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"
