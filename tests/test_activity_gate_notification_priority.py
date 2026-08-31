"""Human-request notifications must not starve behind author churn."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess as sp
import sys

argv = sys.argv[1:]


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

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] in {"pr", "issue", "run"} and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "api":
    endpoint, jq_expr = parse_endpoint_and_jq(argv)
    if endpoint == "notifications":
        notifications = [
            {
                "id": "author-1",
                "reason": "author",
                "updated_at": "2026-08-31T19:50:00Z",
                "subject": {
                    "title": "newer author 1",
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "type": "PullRequest",
                },
                "repository": {"full_name": "org/repo"},
            },
            {
                "id": "author-2",
                "reason": "author",
                "updated_at": "2026-08-31T19:49:00Z",
                "subject": {
                    "title": "newer author 2",
                    "url": "https://api.github.com/repos/org/repo/pulls/2",
                    "type": "PullRequest",
                },
                "repository": {"full_name": "org/repo"},
            },
            {
                "id": "author-3",
                "reason": "author",
                "updated_at": "2026-08-31T19:48:00Z",
                "subject": {
                    "title": "newer author 3",
                    "url": "https://api.github.com/repos/org/repo/pulls/3",
                    "type": "PullRequest",
                },
                "repository": {"full_name": "org/repo"},
            },
            {
                "id": "author-4",
                "reason": "author",
                "updated_at": "2026-08-31T19:47:00Z",
                "subject": {
                    "title": "newer author 4",
                    "url": "https://api.github.com/repos/org/repo/pulls/4",
                    "type": "PullRequest",
                },
                "repository": {"full_name": "org/repo"},
            },
            {
                "id": "mention-newer",
                "reason": "mention",
                "updated_at": "2026-08-31T19:46:00Z",
                "subject": {
                    "title": "newer mention",
                    "url": "https://api.github.com/repos/org/repo/issues/5",
                    "type": "Issue",
                },
                "repository": {"full_name": "org/repo"},
            },
            {
                "id": "mention-buried",
                "reason": "mention",
                "updated_at": "2026-08-31T16:50:00Z",
                "subject": {
                    "title": "buried mention",
                    "url": "https://api.github.com/repos/org/repo/issues/7",
                    "type": "Issue",
                },
                "repository": {"full_name": "org/repo"},
            },
        ]
        print(apply_jq(notifications, jq_expr))
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(tmp: Path, state_dir: Path) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{tmp}:{env['PATH']}"
    (state_dir / "notif-seed.state").write_text("2026-08-01T00:00:00Z")

    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--repo",
            "org/repo",
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
        obj = json.loads(line)
        if obj.get("type") == "notification":
            items.append(obj)
    return items


def test_mentions_emit_before_author_churn_when_notification_cap_applies() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()
        result = _run_gate(tmp, state_dir)

        assert result.returncode in (0, 1), result.stderr
        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 5, result.stdout
        assert [item["detail"] for item in emitted[:2]] == ["mention", "mention"]
        assert [item["title"] for item in emitted[:2]] == [
            "newer mention",
            "buried mention",
        ]
        assert (state_dir / "notif-mention-buried.state").exists()
        assert not (state_dir / "notif-author-4.state").exists()
