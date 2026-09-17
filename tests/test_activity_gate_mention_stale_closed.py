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
    # mention_subject_is_closed() calls repos/{repo}/issues/{number}
    if (f"/issues/{notif_number}" in endpoint
            and "/comments" not in endpoint
            and "/reviews" not in endpoint):
        issue = {"state": issue_state, "number": notif_number}
        print(apply_jq(issue, jq_expr))
        sys.exit(0)
    # Fallback for any other endpoint (comments, reviews, etc.)
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
    env["TEST_ISSUE_STATE"] = issue_state
    env["PATH"] = f"{tmp}:{env['PATH']}"

    # Established state dir: seed a sibling so first-sight emits.
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
