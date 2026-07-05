"""Tests for notification follow-up re-emit in activity-gate.sh.

GitHub re-uses the same notification ID across follow-up comments on the same
thread (only ``updated_at`` advances). Earlier the gate stored a presence-only
state file, which silently dropped every follow-up. The fix: state file stores
the most recently seen ``updated_at`` and re-emits on advance.

Regression for gptme-cloud#195 (Erik's follow-up went unnoticed for 12+ hours
because the original outage-thread mention had already burned the state file).
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

NOTIF_ID = "23717308353"
NOTIF_REPO = "gptme/gptme-cloud"
NOTIF_NUMBER = 195

# Fake gh stub: emits a single mention notification with a parametric updated_at.
# All other gh calls return empty so the gate proceeds quickly to the
# notifications check.
FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess as sp, sys

argv = sys.argv[1:]
notif_updated = os.environ.get("TEST_NOTIF_UPDATED_AT", "2026-04-29T20:40:00Z")
notif_id      = os.environ.get("TEST_NOTIF_ID", "23717308353")
notif_repo    = os.environ.get("TEST_NOTIF_REPO", "gptme/gptme-cloud")
notif_number  = int(os.environ.get("TEST_NOTIF_NUMBER", "195"))

def apply_jq(data, jq_expr):
    if not jq_expr:
        return json.dumps(data)
    r = sp.run(["jq", "-r", jq_expr],
               input=json.dumps(data), capture_output=True, text=True)
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

if not argv:
    sys.exit(2)

if argv[0] == "api":
    endpoint, jq_expr = parse_endpoint_and_jq(argv)
    if "notifications" in endpoint:
        notifs = [{
            "id": notif_id,
            "reason": "mention",
            "updated_at": notif_updated,
            "subject": {
                "title": "outage: fleet.gptme.ai",
                "url": f"https://api.github.com/repos/{notif_repo}/issues/{notif_number}",
                "type": "Issue",
            },
            "repository": {"full_name": notif_repo},
        }]
        print(apply_jq(notifs, jq_expr))
        sys.exit(0)
    print("[]")
    sys.exit(0)

# Everything else returns empty — gate has nothing else to find.
sys.exit(0)
"""


def _run_gate(
    tmp: Path,
    state_dir: Path,
    notif_updated: str,
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["TEST_NOTIF_UPDATED_AT"] = notif_updated
    env["TEST_NOTIF_ID"] = NOTIF_ID
    env["TEST_NOTIF_REPO"] = NOTIF_REPO
    env["TEST_NOTIF_NUMBER"] = str(NOTIF_NUMBER)
    env["PATH"] = f"{tmp}:{env['PATH']}"

    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--org",
            "gptme",
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


def test_first_emit_creates_state_with_timestamp() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        result = _run_gate(tmp, state_dir, "2026-04-29T20:40:00Z")
        assert result.returncode in (0, 1), result.stderr

        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1
        assert emitted[0]["repo"] == NOTIF_REPO
        assert emitted[0]["number"] == NOTIF_NUMBER

        state_file = state_dir / f"notif-{NOTIF_ID}.state"
        assert state_file.exists()
        # State now records the timestamp, not just presence.
        assert state_file.read_text().strip() == "2026-04-29T20:40:00Z"


def test_same_updated_at_does_not_reemit() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Prior run already saw this exact updated_at.
        (state_dir / f"notif-{NOTIF_ID}.state").write_text("2026-04-29T20:40:00Z")

        result = _run_gate(tmp, state_dir, "2026-04-29T20:40:00Z")
        assert result.returncode in (0, 1), result.stderr

        emitted = _emitted_notifications(result.stdout)
        assert emitted == [], (
            "Expected no re-emit when updated_at unchanged, got: " f"{emitted}"
        )


def test_advanced_updated_at_reemits_followup() -> None:
    """Erik's #195 case: notification ID stable, updated_at advanced → re-emit."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        # Original mention from yesterday already processed.
        (state_dir / f"notif-{NOTIF_ID}.state").write_text("2026-04-29T20:40:00Z")

        # Erik posts a follow-up comment 12h later — same notif ID, newer updated_at.
        result = _run_gate(tmp, state_dir, "2026-04-30T10:01:28Z")
        assert result.returncode in (0, 1), result.stderr

        emitted = _emitted_notifications(result.stdout)
        assert len(emitted) == 1, (
            "Expected re-emit when updated_at advances, got: " f"{emitted}"
        )
        assert emitted[0]["number"] == NOTIF_NUMBER

        # State file rolled forward to the newer timestamp.
        assert (state_dir / f"notif-{NOTIF_ID}.state").read_text().strip() == (
            "2026-04-30T10:01:28Z"
        )


def test_older_updated_at_does_not_reemit() -> None:
    """Defensive: a stale fetch returning an older updated_at must not re-emit."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        state_dir = tmp / "state"
        state_dir.mkdir()

        (state_dir / f"notif-{NOTIF_ID}.state").write_text("2026-04-30T10:01:28Z")

        result = _run_gate(tmp, state_dir, "2026-04-29T20:40:00Z")
        assert result.returncode in (0, 1), result.stderr

        assert _emitted_notifications(result.stdout) == []
        # State unchanged.
        assert (state_dir / f"notif-{NOTIF_ID}.state").read_text().strip() == (
            "2026-04-30T10:01:28Z"
        )
