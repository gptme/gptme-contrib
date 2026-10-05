"""Mention notifications on already-closed/merged threads must not dispatch.

GitHub keeps notifications unread indefinitely after closure — a mention from
the original issue body can look "fresh" (updated_at advances on any follow-up)
long after the issue was resolved and closed. Dispatching those produces NOOP
sessions that conflict with the per-dispatch mandate (no-comment on closed threads).

This test verifies the drop-only staleness filter added in
gptme-contrib#1676 (sessions 6a1e/c777, 2026-09-17):
 - mention on a CLOSED issue/PR → state file written, item NOT emitted (jsonl + markdown)
 - mention on an OPEN issue/PR  → still emitted (normal path)
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

NOTIF_ID = "25700000001"
NOTIF_REPO = "ActivityWatch/aw-server-rust"
NOTIF_NUMBER = 688

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess as sp, sys

argv = sys.argv[1:]
notif_updated = os.environ.get("TEST_NOTIF_UPDATED_AT", "2026-09-17T11:00:00Z")
notif_id = os.environ.get("TEST_NOTIF_ID", "25700000001")
notif_repo = os.environ.get("TEST_NOTIF_REPO", "ActivityWatch/aw-server-rust")
notif_number = int(os.environ.get("TEST_NOTIF_NUMBER", "688"))
notif_reason = os.environ.get("TEST_NOTIF_REASON", "mention")
subject_type = os.environ.get("TEST_SUBJECT_TYPE", "Issue")
issue_state = os.environ.get("TEST_ISSUE_STATE", "open")
latest_actor = os.environ.get("TEST_LATEST_ACTOR", "test-author")
latest_actor_type = os.environ.get("TEST_LATEST_ACTOR_TYPE", "")
latest_actor_surface = os.environ.get("TEST_LATEST_ACTOR_SURFACE", "comment")
latest_actor_missing_user = os.environ.get("TEST_LATEST_ACTOR_MISSING_USER", "") == "1"
human_before_latest = os.environ.get("TEST_HUMAN_BEFORE_LATEST", "")
human_before_latest_at = os.environ.get(
    "TEST_HUMAN_BEFORE_LATEST_AT", "2026-09-17T10:00:00Z"
)
human_before_latest_updated_at = os.environ.get(
    "TEST_HUMAN_BEFORE_LATEST_UPDATED_AT", human_before_latest_at
)
older_comment_actor = os.environ.get("TEST_OLDER_COMMENT_ACTOR", "")


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
        if args[i] in ("--paginate", "--silent", "--slurp"):
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
                "title": "aw-sync: duplicate folders for one device_id",
                "url": f"https://api.github.com/repos/{notif_repo}/issues/{notif_number}",
                "type": subject_type,
            },
            "repository": {"full_name": notif_repo},
        }]
        print(apply_jq(notifs, jq_expr))
        sys.exit(0)
    # notification_subject_is_closed() calls repos/{repo}/issues/{number}
    if (f"/issues/{notif_number}" in endpoint
            and "/comments" not in endpoint
            and "/reviews" not in endpoint):
        issue = {"state": issue_state, "number": notif_number}
        print(apply_jq(issue, jq_expr))
        sys.exit(0)
    if f"/issues/{notif_number}/comments" in endpoint:
        comments = []
        if older_comment_actor:
            comments.append(
                {
                    "user": {"login": older_comment_actor, "type": "User"},
                    "created_at": "2026-09-17T09:30:00Z",
                }
            )
        if human_before_latest:
            comments.append(
                {
                    "user": {"login": human_before_latest, "type": "User"},
                    "created_at": human_before_latest_at,
                    "updated_at": human_before_latest_updated_at,
                }
            )
        if latest_actor_surface == "comment":
            comments.append(
                {
                    "user": (
                        None
                        if latest_actor_missing_user
                        else {"login": latest_actor, "type": latest_actor_type}
                    ),
                    "created_at": "2026-09-17T11:00:00Z",
                }
            )
        pages = [comments]
        print(apply_jq(pages, jq_expr) if jq_expr else json.dumps(pages))
        sys.exit(0)
    if f"/pulls/{notif_number}/reviews" in endpoint:
        reviews = []
        if latest_actor_surface == "review":
            reviews = [{"user": {"login": latest_actor, "type": latest_actor_type}, "submitted_at": "2026-09-17T10:00:00Z"}]
        pages = [reviews]
        print(apply_jq(pages, jq_expr) if jq_expr else json.dumps(pages))
        sys.exit(0)
    if f"/pulls/{notif_number}/comments" in endpoint:
        comments = []
        if latest_actor_surface == "inline":
            comments = [{"user": {"login": latest_actor, "type": latest_actor_type}, "created_at": "2026-09-17T10:00:00Z"}]
        pages = [comments]
        print(apply_jq(pages, jq_expr) if jq_expr else json.dumps(pages))
        sys.exit(0)
    # Fallback for any other endpoint.
    if "--paginate" in argv or "--slurp" in argv:
        print(apply_jq([[]], jq_expr) if jq_expr else "[[]]")
    else:
        print(apply_jq([], jq_expr) if jq_expr else "[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    *,
    reason: str = "mention",
    subject_type: str = "Issue",
    issue_state: str = "open",
    latest_actor: str = "test-author",
    latest_actor_type: str = "",
    latest_actor_surface: str = "comment",
    latest_actor_missing_user: bool = False,
    human_before_latest: str = "",
    human_before_latest_at: str = "2026-09-17T10:00:00Z",
    human_before_latest_updated_at: str | None = None,
    older_comment_actor: str = "",
    prior: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    # Keep actor classification deterministic when the developer's shell has a
    # different live bot identity configured.
    env["BOT_USERNAME"] = "test-author"
    env["TEST_NOTIF_ID"] = NOTIF_ID
    env["TEST_NOTIF_REPO"] = NOTIF_REPO
    env["TEST_NOTIF_NUMBER"] = str(NOTIF_NUMBER)
    env["TEST_NOTIF_REASON"] = reason
    env["TEST_SUBJECT_TYPE"] = subject_type
    env["TEST_ISSUE_STATE"] = issue_state
    env["TEST_NOTIF_UPDATED_AT"] = "2026-09-17T11:00:00Z"
    env["TEST_LATEST_ACTOR"] = latest_actor
    env["TEST_LATEST_ACTOR_TYPE"] = latest_actor_type
    env["TEST_LATEST_ACTOR_SURFACE"] = latest_actor_surface
    env["TEST_LATEST_ACTOR_MISSING_USER"] = "1" if latest_actor_missing_user else "0"
    env["TEST_HUMAN_BEFORE_LATEST"] = human_before_latest
    env["TEST_HUMAN_BEFORE_LATEST_AT"] = human_before_latest_at
    env["TEST_HUMAN_BEFORE_LATEST_UPDATED_AT"] = (
        human_before_latest_updated_at or human_before_latest_at
    )
    env["TEST_OLDER_COMMENT_ACTOR"] = older_comment_actor
    env["PATH"] = f"{tmp}:{env['PATH']}"

    # Established state dir: seed a sibling so first-sight emits.
    (state_dir / "notif-99999999999.state").write_text("2026-09-01T00:00:00Z")
    if prior is not None:
        (state_dir / f"notif-{NOTIF_ID}.state").write_text(prior)

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


def test_comment_first_sight_on_closed_issue_emits() -> None:
    """Never swallow older human work when this notification has no watermark."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="codecov[bot]",
        )
        assert result.returncode in (0, 1), result.stderr
        assert len(_emitted_notifications(result.stdout)) == 1, result.stdout


def test_comment_bot_update_on_closed_issue_is_suppressed() -> None:
    """After a prior dispatch, a bot-only closed-thread bump is noise."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="codecov[bot]",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        assert _emitted_notifications(result.stdout) == [], result.stdout


def test_unknown_actor_after_bot_on_closed_issue_emits() -> None:
    """An unclassifiable latest actor must keep the notification visible."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor_missing_user=True,
            older_comment_actor="codecov[bot]",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        assert len(_emitted_notifications(result.stdout)) == 1, result.stdout


def test_human_comment_after_prior_then_bot_on_closed_issue_emits() -> None:
    """A later bot must not hide human work newer than the watermark."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="codecov[bot]",
            human_before_latest="maintainer",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout


def test_human_comment_at_prior_timestamp_then_bot_on_closed_issue_emits() -> None:
    """A timestamp tie with the prior watermark must fail open for human work."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="codecov[bot]",
            human_before_latest="maintainer",
            human_before_latest_at="2026-09-17T09:00:00Z",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        assert len(_emitted_notifications(result.stdout)) == 1, result.stdout


def test_edited_human_comment_after_prior_then_bot_on_closed_issue_emits() -> None:
    """A human edit after the watermark must count as fresh human activity."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="codecov[bot]",
            human_before_latest="maintainer",
            human_before_latest_at="2026-09-17T08:00:00Z",
            human_before_latest_updated_at="2026-09-17T10:00:00Z",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        assert len(_emitted_notifications(result.stdout)) == 1, result.stdout


def test_bot_shaped_user_comment_on_closed_issue_emits() -> None:
    """An explicit REST User type outranks bot-like login heuristics."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            issue_state="closed",
            latest_actor="renovate-helper",
            latest_actor_type="User",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        assert len(_emitted_notifications(result.stdout)) == 1, result.stdout


def test_inline_human_comment_on_closed_pr_emits() -> None:
    """A human inline reply must outrank an older self issue comment."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="comment",
            subject_type="PullRequest",
            issue_state="closed",
            latest_actor="maintainer",
            latest_actor_type="User",
            latest_actor_surface="inline",
            older_comment_actor="test-author",
            prior="2026-09-17T09:00:00Z",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, result.stdout


def test_mention_suppressed_when_issue_is_closed() -> None:
    """A mention on a closed issue must not dispatch — no actionable work exists."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, reason="mention", issue_state="closed")
        assert result.returncode in (0, 1), result.stderr
        assert (
            _emitted_notifications(result.stdout) == []
        ), f"Expected no dispatch on closed issue, got: {result.stdout!r}"
        # State file must be persisted so the item is not retried next run.
        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert state_file.exists(), "State file must be written even when skipped"
        assert state_file.read_text().strip() == "2026-09-17T11:00:00Z"


def test_mention_suppressed_when_pr_is_merged() -> None:
    """A mention on a merged/closed PR must not dispatch.

    GitHub's issues API returns state='closed' for merged PRs, so the same
    check covers both merged and manually-closed PRs.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="mention",
            subject_type="PullRequest",
            issue_state="closed",
        )
        assert result.returncode in (0, 1), result.stderr
        assert (
            _emitted_notifications(result.stdout) == []
        ), f"Expected no dispatch on closed PR, got: {result.stdout!r}"
        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert state_file.exists()


def test_mention_emits_when_issue_is_open() -> None:
    """A mention on an OPEN issue must still reach the dispatcher."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir, reason="mention", issue_state="open")
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert (
            len(emitted) == 1
        ), f"Expected mention on open issue to dispatch, got: {result.stdout!r}"
        assert emitted[0]["detail"] == "mention"


def test_mention_emits_when_pr_is_open() -> None:
    """A mention on an open PR must still reach the dispatcher."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(
            tmp,
            state_dir,
            reason="mention",
            subject_type="PullRequest",
            issue_state="open",
        )
        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert (
            len(emitted) == 1
        ), f"Expected mention on open PR to dispatch, got: {result.stdout!r}"
        assert emitted[0]["detail"] == "mention"


def _run_gate_markdown(
    tmp: Path,
    state_dir: Path,
    *,
    reason: str = "mention",
    subject_type: str = "Issue",
    issue_state: str = "open",
) -> subprocess.CompletedProcess[str]:
    """Same as _run_gate but with --format markdown (the default used by cron previews)."""
    fake_gh = tmp / "gh"
    if not fake_gh.exists():
        fake_gh.write_text(FAKE_GH)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_NOTIF_ID"] = NOTIF_ID
    env["TEST_NOTIF_REPO"] = NOTIF_REPO
    env["TEST_NOTIF_NUMBER"] = str(NOTIF_NUMBER)
    env["TEST_NOTIF_REASON"] = reason
    env["TEST_SUBJECT_TYPE"] = subject_type
    env["TEST_ISSUE_STATE"] = issue_state
    env["PATH"] = f"{tmp}:{env['PATH']}"

    (state_dir / "notif-99999999999.state").write_text("2026-09-01T00:00:00Z")

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
            "markdown",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_markdown_mention_suppressed_when_issue_is_closed() -> None:
    """Markdown branch: a mention on a closed issue must not add to the actionable count."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate_markdown(
            tmp, state_dir, reason="mention", issue_state="closed"
        )
        assert result.returncode in (0, 1), result.stderr
        # Closed mention must not appear in the markdown notification line.
        # Either no notification line at all, or the count must be 0 actionable.
        assert (
            "notifications" not in result.stdout or "0 actionable" in result.stdout
        ), f"Unexpected notification count in markdown output: {result.stdout!r}"
        # State file must still be persisted (so the item is not retried).
        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert (
            state_file.exists()
        ), "State file must be written even when suppressed in markdown mode"


def test_markdown_mention_emits_when_issue_is_open() -> None:
    """Markdown branch: an open-issue mention must still count as actionable."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate_markdown(
            tmp, state_dir, reason="mention", issue_state="open"
        )
        assert result.returncode in (0, 1), result.stderr
        # An open mention must produce a non-zero notification count.
        assert (
            "notifications" in result.stdout
        ), f"Expected open mention to appear in markdown output, got: {result.stdout!r}"
