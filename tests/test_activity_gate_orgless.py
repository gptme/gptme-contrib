"""Regression tests for org-less repo-list mode in activity-gate.sh.

Context (the 2026-07-08..11 PM outage): Bob's brain dropped --org from the
activity-gate invocation to avoid a full org scan, passing only --repo flags.
activity-gate.sh hard-required BOTH --author and --org and exited 2, so every
monitoring cycle's activity scan failed and the caller failed open to zero
items — pr_update / ci_failure / merge_ready / greptile / assigned_issue /
notification detection was silently dead for 3 days.

contrib#1272 made --org optional when at least one --repo is given. These
tests pin that invocation contract:

- --author + --repo (no --org) is a valid invocation: exit 0/1, no usage
  error, and no org enumeration (`gh repo list` must not be called).
- --author alone (no --org, no --repo) is still a usage error (exit 2).
- --author + --org (no --repo) still enumerates the org.
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

# Fake gh: records every invocation's first two args to GH_CALL_LOG, returns
# empty/benign payloads everywhere so the gate scans cleanly and exits 1.
FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations
import os, sys
from pathlib import Path

argv = sys.argv[1:]
log = os.environ.get("GH_CALL_LOG", "")
if log:
    with Path(log).open("a") as fh:
        fh.write(" ".join(argv[:2]) + "\n")

if not argv:
    sys.exit(2)

if argv[0] == "repo" and len(argv) > 1 and argv[1] == "list":
    print("fakeorg/repo-a\nfakeorg/repo-b")
    sys.exit(0)

if argv[0] in ("pr", "issue", "run") and len(argv) > 1 and argv[1] == "list":
    print("[]")
    sys.exit(0)

if argv[0] == "api":
    # gh api calls here use --jq server-side filtering; the gate expects the
    # FILTERED output. notifications must yield nothing (no actionable
    # notifs), not a raw "[]" — a bare [] flows into the gate's jq object
    # filter and causes a runtime error (jq exit 5) under set -o pipefail.
    if "notifications" in (argv[1] if len(argv) > 1 else ""):
        sys.exit(0)
    print("[]")
    sys.exit(0)

sys.exit(0)
"""


def _run_gate(
    extra_args: list[str], tmp: Path
) -> tuple[subprocess.CompletedProcess, list[str]]:
    state_dir = tmp / "state"
    state_dir.mkdir(exist_ok=True)

    fake_gh = tmp / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    call_log = tmp / "gh-calls.log"

    env = os.environ.copy()
    env["PATH"] = f"{tmp}:{env['PATH']}"
    env["GH_CALL_LOG"] = str(call_log)

    result = subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--state-dir",
            str(state_dir),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    return result, calls


def test_orgless_repo_mode_is_a_valid_invocation() -> None:
    """--author + --repo (no --org) must scan the listed repos, not exit 2."""
    with tempfile.TemporaryDirectory() as tmp_str:
        result, calls = _run_gate(
            ["--repo", "owner/repo-x", "--repo", "owner/repo-y", "--format", "jsonl"],
            Path(tmp_str),
        )

        assert result.returncode in (0, 1), (
            f"org-less --repo invocation must be valid (exit 0/1), got "
            f"{result.returncode}; stderr: {result.stderr!r}"
        )
        assert (
            "required" not in result.stderr
        ), f"must not report a usage error: {result.stderr!r}"
        # Both explicit repos were scanned (PR data fetched per repo).
        pr_calls = [c for c in calls if c == "pr list"]
        assert len(pr_calls) >= 2, f"expected per-repo pr list calls, got: {calls}"


def test_orgless_repo_mode_skips_org_enumeration() -> None:
    """Without --org, `gh repo list` (the org scan) must never be called."""
    with tempfile.TemporaryDirectory() as tmp_str:
        result, calls = _run_gate(
            ["--repo", "owner/repo-x", "--format", "jsonl"], Path(tmp_str)
        )

        assert result.returncode in (0, 1), result.stderr
        assert "repo list" not in calls, (
            f"org enumeration must be skipped in org-less mode "
            f"(that is the GraphQL saving the mode exists for); calls: {calls}"
        )


def test_no_org_and_no_repo_is_still_a_usage_error() -> None:
    """--author alone has nothing to scan — must exit 2 with a clear error."""
    with tempfile.TemporaryDirectory() as tmp_str:
        result, _ = _run_gate(["--format", "jsonl"], Path(tmp_str))

        assert result.returncode == 2, (
            f"expected usage error exit 2, got {result.returncode}; "
            f"stdout: {result.stdout!r}"
        )
        assert "--org or at least one --repo" in result.stderr


def test_org_mode_still_enumerates_org() -> None:
    """--org without --repo keeps the original org-scan behaviour."""
    with tempfile.TemporaryDirectory() as tmp_str:
        result, calls = _run_gate(
            ["--org", "fakeorg", "--format", "jsonl"], Path(tmp_str)
        )

        assert result.returncode in (0, 1), result.stderr
        assert (
            "repo list" in calls
        ), f"--org mode must enumerate org repos via gh repo list; calls: {calls}"
        # Enumerated repos are then scanned.
        assert "pr list" in calls, f"org repos must be scanned; calls: {calls}"


def test_orgless_output_is_well_formed_jsonl_when_work_found() -> None:
    """Sanity: org-less mode emits parseable JSONL items (assigned-issue path
    emits on first sight when someone else holds the ball)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        # Extend the fake gh: one assigned issue awaiting reply.
        fake = FAKE_GH.replace(
            'if argv[0] in ("pr", "issue", "run") and len(argv) > 1 and argv[1] == "list":\n    print("[]")\n    sys.exit(0)',
            'if argv[0] == "issue" and len(argv) > 1 and argv[1] == "list":\n'
            '    print(\'[{"number": 7, "title": "t", "updatedAt": "2026-07-11T00:00:00Z"}]\')\n'
            "    sys.exit(0)\n"
            'if argv[0] in ("pr", "run") and len(argv) > 1 and argv[1] == "list":\n'
            '    print("[]")\n'
            "    sys.exit(0)",
        )
        # Last commenter on the issue is someone else -> awaiting our reply.
        fake = fake.replace(
            '    print("[]")\n    sys.exit(0)\n\nsys.exit(0)',
            '    if "comments" in (argv[1] if len(argv) > 1 else ""):\n'
            '        print("someone-else")\n'
            "        sys.exit(0)\n"
            '    print("[]")\n'
            "    sys.exit(0)\n\nsys.exit(0)",
        )
        state_dir = tmp / "state"
        state_dir.mkdir()
        fake_gh = tmp / "gh"
        fake_gh.write_text(fake)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"

        result = subprocess.run(
            [
                str(SCRIPT),
                "--author",
                "test-author",
                "--repo",
                "owner/repo-x",
                "--state-dir",
                str(state_dir),
                "--format",
                "jsonl",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, (
            f"expected work found (exit 0), got {result.returncode}; "
            f"stderr: {result.stderr!r}"
        )
        items = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert any(
            i["type"] == "assigned_issue" and i["repo"] == "owner/repo-x" for i in items
        ), f"expected assigned_issue item, got: {items}"
