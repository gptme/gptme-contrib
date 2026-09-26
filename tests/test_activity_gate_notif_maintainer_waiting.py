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
# Optional override for the bot waiting-comment body (e.g. the PM human-merge
# marker body) — without it the fake gh only ever serves the canonical phrase.
waiting_body = os.environ.get("TEST_WAITING_BODY")
# Current PR head sha served for `repos/<repo>/pulls/<n>` lookups (used by the
# head-scoped PM human-merge marker check).
head_sha = os.environ.get(
    "TEST_HEAD_SHA", "a65ead926a4080f6a17de9af384a6a87774f5761"
)
# Human activity after the bot's waiting comment reopens the handoff.
human_after_waiting = os.environ.get("TEST_HUMAN_AFTER_WAITING", "0")
bot_after_waiting = os.environ.get("TEST_BOT_AFTER_WAITING", "0")
human_review_after_waiting = os.environ.get("TEST_HUMAN_REVIEW_AFTER_WAITING", "0")
# Bot re-affirms the handoff AFTER a human comment (regression scenario for last→first fix).
bot_reaffirm_waiting = os.environ.get("TEST_BOT_REAFFIRM_WAITING", "0")
comment_count = int(os.environ.get("TEST_COMMENT_COUNT", "1"))
# Identity that authored the waiting handoff. Defaults to Bob; forks whose
# `--author` differs (e.g. Alice) serve their own login so the gate must
# recognise it as a bot identity rather than anonymous human activity.
comment_author = os.environ.get("TEST_COMMENT_AUTHOR", "TimeToBuildBob")


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
    if "/issues/" in endpoint and "/comments" in endpoint:
        if waiting_comment == "1":
            # The waiting comment is posted by the agent itself (`gh` auth =
            # the handle it passes as --author), so the fixture's bot login
            # matches the gate's --author. This mirrors the fork-general
            # reality where BOT_USERNAME defaults to --author.
            comments = [{
                "user": {"login": comment_author, "type": "User"},
                "body": waiting_body
                or "CI-green and mergeable — waiting only on a maintainer click.",
                "created_at": "2026-08-26T16:00:00Z",
            }]
            comments.extend(
                {
                    "user": {"login": "codecov[bot]", "type": "Bot"},
                    "body": f"Coverage report {index}",
                    "created_at": "2026-08-26T16:30:00Z",
                }
                for index in range(1, comment_count)
            )
            if bot_after_waiting == "1":
                comments.append({
                    "user": {"login": "codecov[bot]", "type": "Bot"},
                    "body": "Coverage report",
                    "created_at": "2026-08-26T16:30:00Z",
                })
            if human_after_waiting == "1":
                # A maintainer replied after the bot's waiting comment, e.g. asking
                # for a docs update.  The human's comment is now the latest — the
                # notification must NOT be suppressed.
                comments.append({
                    "user": {"login": "ErikBjare", "type": "User"},
                    "body": "Please also update the changelog before merging.",
                    "created_at": "2026-08-26T17:00:00Z",
                })
            if bot_reaffirm_waiting == "1":
                # Bot re-posts the waiting handoff AFTER a human comment (e.g. CI
                # re-ran and the bot re-affirmed the green state). With `last` this
                # second handoff was selected, finding no human after it and wrongly
                # suppressing the notification. With `first` the original handoff
                # is selected; the human at 17:00 is after 16:00, so it emits.
                comments.append({
                    "user": {"login": comment_author, "type": "User"},
                    "body": "CI is green again — waiting only on a maintainer click.",
                    "created_at": "2026-08-26T17:30:00Z",
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
    if "/pulls/" in endpoint and endpoint.endswith(str(notif_number)):
        pr = {"head": {"sha": head_sha}}
        print(apply_jq(pr, jq_expr))
        sys.exit(0)
    if endpoint.endswith("/reviews?per_page=100"):
        reviews = []
        if human_review_after_waiting == "1":
            reviews.append({
                "user": {"login": "ErikBjare", "type": "User"},
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-08-26T17:00:00Z",
            })
        pages = [reviews[index:index + 100] for index in range(0, len(reviews), 100)]
        print(apply_jq(pages, jq_expr) if jq_expr else json.dumps(pages))
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
    bot_after_waiting: str = "0",
    human_review_after_waiting: str = "0",
    bot_reaffirm_waiting: str = "0",
    comment_count: int = 1,
    waiting_body: str | None = None,
    head_sha: str | None = None,
    author: str = "test-author",
    comment_author: str = "TimeToBuildBob",
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
    if waiting_body is not None:
        env["TEST_WAITING_BODY"] = waiting_body
    if head_sha is not None:
        env["TEST_HEAD_SHA"] = head_sha
    env["TEST_HUMAN_AFTER_WAITING"] = human_after_waiting
    env["TEST_BOT_AFTER_WAITING"] = bot_after_waiting
    env["TEST_HUMAN_REVIEW_AFTER_WAITING"] = human_review_after_waiting
    env["TEST_BOT_REAFFIRM_WAITING"] = bot_reaffirm_waiting
    env["TEST_COMMENT_COUNT"] = str(comment_count)
    env["TEST_COMMENT_AUTHOR"] = comment_author
    # Hermetic: the gate must resolve its own identity from --author, not an
    # ambient BOT_USERNAME exported in the developer's shell.
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


def test_author_pr_notification_suppressed_when_bot_commented_after_waiting() -> None:
    """A later automation comment must not reopen a completed handoff."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, bot_after_waiting="1")
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout


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


def test_author_pr_emits_when_human_review_follows_waiting_comment() -> None:
    """A submitted maintainer review is activity even without an issue comment."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, human_review_after_waiting="1")
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


def test_author_pr_emits_when_bot_reaffirms_after_human_comment() -> None:
    """Regression for last→first fix: bot re-posts handoff after human request.

    Concrete scenario (the P1 finding from AI review of #1522):
    - Bot posts "waiting only on a maintainer click" at T=10:00 (first handoff).
    - Maintainer replies "please update docs" at T=10:05 (human activity).
    - Bot re-posts the same phrase at T=10:10 after a CI re-run (second handoff).

    With ``last``: the second handoff at T=10:10 is selected as the reference; no
    human comment follows it → suppress=True.  The maintainer's request is silently
    dropped — the bug.

    With ``first``: the original handoff at T=10:00 is the reference; the human
    comment at T=10:05 is after it → suppress=False → notification emits correctly.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            waiting="1",
            human_after_waiting="1",
            bot_reaffirm_waiting="1",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"


PM_HUMAN_MERGE_BODY = (
    "Automated merge handoff for `a65ead926a40`:\n\n"
    "This head is ready for maintainer review and **manual merge**. Project\n"
    "monitoring marked it `human_merge_required`.\n\n"
    "<!-- bob-pm-human-merge-required head=a65ead926a4080f6a17de9af384a6a87774f5761 -->"
)


def test_human_merge_marker_suppresses_author_notification() -> None:
    """PM's canonical human-merge handoff marker must count as a waiting handoff.

    ``bob-pm-human-merge-required`` is the head-scoped marker PM posts for a
    path-policy head whose sole remaining gate is a maintainer merge. It was not
    in the suppression phrase set, so author notifications kept emitting on every
    Codecov/Greptile re-unread and dispatched sessions that could only re-confirm
    the handoff that already existed (gptme/gptme-cloud#973).
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, waiting_body=PM_HUMAN_MERGE_BODY)
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout


def test_human_merge_marker_reopens_on_human_comment() -> None:
    """A maintainer comment after the human-merge handoff must still emit.

    Guards against over-suppression: recognising the PM marker must not turn the
    handoff into a permanent silence when a human actually replies.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            waiting_body=PM_HUMAN_MERGE_BODY,
            human_after_waiting="1",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"


def test_author_identity_handoff_suppresses_without_bot_username() -> None:
    """A handoff comment authored by `--author` must suppress, not re-open.

    Forks that pass `--author` but not the ``BOT_USERNAME`` override (Alice's
    monitoring harness passes ``--author TimeToLearnAlice``) authored the
    handoff as a login the gate did not recognise. Two failures followed:
    ``has_maintainer_waiting_comment`` never armed, and
    ``latest_comment_is_bot_waiting`` treated the gate's own comment as
    anonymous human activity. The result was the same handoff comment re-posted
    every cooldown cycle (7 identical posts on gptme/gptme-contrib#1700).
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            author="TimeToLearnAlice",
            comment_author="TimeToLearnAlice",
        )
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout


def test_author_identity_handoff_reopens_on_human_comment() -> None:
    """Author-identity recognition must not silence a later human reply."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            author="TimeToLearnAlice",
            comment_author="TimeToLearnAlice",
            human_after_waiting="1",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout
        assert emitted[0]["detail"] == "author"
