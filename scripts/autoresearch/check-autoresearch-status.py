#!/usr/bin/env python3
"""Check autoresearch branch status for the operator.

Detects if there are new commits on autoresearch/eval-improvement-* branches
in the artifact repo since the last operator review. Reports score deltas and
budget consumption across all active experiments.

Used by the operator prompt during Phase 1 diagnostics.

Configuration via environment variables:
  ARTIFACT_DIR — path to the artifact repo (default: current directory)
  AUTORESEARCH_STATE_DIR — path to autoresearch state (default: <git-repo-root>/state/autoresearch)
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", os.getcwd()))


def _default_state_dir() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        return str(Path(result.stdout.strip()) / "state" / "autoresearch")
    except subprocess.CalledProcessError:
        return str(Path(os.getcwd()) / "state" / "autoresearch")


STATE_DIR = Path(os.environ.get("AUTORESEARCH_STATE_DIR", _default_state_dir()))
LAST_REVIEW_FILE = STATE_DIR / "last-operator-review.txt"
BRANCH_PREFIX = "autoresearch/eval-improvement"
BUDGET_DIR = STATE_DIR / "budget"


def get_autoresearch_branches() -> list[str]:
    """List all autoresearch branches in artifact repo."""
    if not ARTIFACT_DIR.exists():
        return []
    result = subprocess.run(
        ["git", "branch", "-r", "--list", f"origin/{BRANCH_PREFIX}*"],
        cwd=ARTIFACT_DIR,
        capture_output=True,
        text=True,
    )
    branches = [
        b.strip().removeprefix("origin/")
        for b in result.stdout.splitlines()
        if b.strip()
    ]
    return branches


def get_branch_latest_commit(branch: str) -> tuple[str, str]:
    """Return (commit_hash, commit_date) for the latest commit on branch."""
    result = subprocess.run(
        ["git", "log", f"origin/{branch}", "--format=%H %ci", "-1"],
        cwd=ARTIFACT_DIR,
        capture_output=True,
        text=True,
    )
    parts = result.stdout.strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""


def get_last_review_time() -> str:
    """Return ISO timestamp of last operator review (or epoch if never)."""
    if LAST_REVIEW_FILE.exists():
        return LAST_REVIEW_FILE.read_text().strip()
    return "1970-01-01T00:00:00+00:00"


def get_score_delta_from_log() -> str:
    """Read the latest session log and extract score progression."""
    logs = list(STATE_DIR.glob("session_*_iter_*.log"))
    if not logs:
        return "no logs found"
    latest = max(logs, key=lambda p: p.stat().st_mtime)
    lines = latest.read_text().splitlines()
    score_lines = [
        line
        for line in lines
        if any(
            kw in line for kw in ["Baseline:", "✅ ACCEPTED", "❌ REJECTED", "Score:"]
        )
    ]
    return "\n".join(score_lines[-15:]) if score_lines else "no score data"


def count_active_iterations() -> int:
    """Count unique in-progress iteration numbers for today from session log files.

    Counts main iter_N.log files (excludes candidate/model/failure variants).
    Used when budget tracking file doesn't exist yet (session still in progress).
    """
    today = datetime.now(UTC).strftime("%Y%m%d")
    seen: set[tuple[str, int]] = set()
    # Match main iter logs only: session_YYYYMMDD_HHMMSS_iter_N.log
    pattern = re.compile(rf"session_{today}_\d+_iter_(\d+)\.log$")
    for f in STATE_DIR.glob(f"session_{today}_*_iter_*.log"):
        m = pattern.match(f.name)
        if m:
            session_ts = f.name.split("_iter_")[0]
            seen.add((session_ts, int(m.group(1))))
    return len(seen)


def get_budget_summary() -> str:
    """Show today's budget consumption across all experiments, plus all-time totals."""
    if not BUDGET_DIR.exists():
        return "no budget data"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    budget_files = list(BUDGET_DIR.glob(f"*-{today}.json"))
    lines = []
    total_used = 0
    if not budget_files:
        active = count_active_iterations()
        if active > 0:
            lines.append(
                f"  (session in progress: {active} iterations today, budget file not yet written)"
            )
        else:
            lines.append(f"  (no budget data for today, {today})")
    for bf in sorted(budget_files):
        experiment = bf.stem.replace(f"-{today}", "")
        if experiment == "global":
            continue
        try:
            d = json.loads(bf.read_text())
            used = d.get("iterations", 0)
            total_used += used
            lines.append(f"  {experiment}: {used} iterations today")
        except (json.JSONDecodeError, OSError):
            lines.append(f"  {experiment}: (error reading budget)")
    # Check global budget if it exists
    global_file = BUDGET_DIR / f"global-{today}.json"
    if global_file.exists():
        try:
            gd = json.loads(global_file.read_text())
            global_used = gd.get("iterations", 0)
            global_limit = gd.get("limit", "?")
            lines.append(
                f"  GLOBAL: {global_used}/{global_limit} total iterations today"
            )
        except (json.JSONDecodeError, OSError):
            pass
    elif budget_files:
        lines.append(f"  TOTAL: {total_used} iterations across all experiments today")
    # Show all-time totals for experiments with total_budget configured
    all_time_files = list(BUDGET_DIR.glob("*-all-time.json"))
    for atf in sorted(all_time_files):
        experiment = atf.stem.replace("-all-time", "")
        try:
            d = json.loads(atf.read_text())
            total_iter = d.get("total_iterations", 0)
            limit = d.get("limit", "?")
            pct = (
                f"{100 * total_iter / limit:.0f}%"
                if isinstance(limit, int) and limit > 0
                else "?"
            )
            status = (
                " ✅ COMPLETE" if isinstance(limit, int) and total_iter >= limit else ""
            )
            lines.append(
                f"  {experiment} all-time: {total_iter}/{limit} ({pct}){status}"
            )
        except (json.JSONDecodeError, OSError):
            pass
    return "\n".join(lines) if lines else "no data"


def get_service_status() -> str:
    """Check which autoresearch services are running.

    Looks for systemd services matching *autoresearch*. Agent-specific
    service naming (e.g. bob-autoresearch*, alice-autoresearch*) is
    handled by the glob pattern.
    """
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--state=active",
            "--no-legend",
            "*autoresearch*",
        ],
        capture_output=True,
        text=True,
    )
    active = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    if not active:
        return "no autoresearch services running"
    return f"{len(active)} service(s) running: {', '.join(active)}"


def update_review_timestamp() -> None:
    """Mark current time as last operator review."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    LAST_REVIEW_FILE.write_text(now)


def main() -> int:
    # Always show service and budget status (health indicators)
    print(f"autoresearch services: {get_service_status()}")
    print(f"autoresearch budget:\n{get_budget_summary()}")
    print()

    branches = get_autoresearch_branches()
    if not branches:
        print("autoresearch: no improvement branches found in artifact repo")
        return 0

    last_review = get_last_review_time()
    new_branches = []

    for branch in branches:
        commit_hash, commit_date = get_branch_latest_commit(branch)
        if commit_date > last_review:
            new_branches.append((branch, commit_hash, commit_date))

    if not new_branches:
        print(
            f"autoresearch: {len(branches)} branch(es) found, no new commits since last review ({last_review[:10]})"
        )
        return 0

    print(
        f"⚠️  autoresearch: {len(new_branches)} branch(es) with new commits since last review!"
    )
    for branch, commit_hash, commit_date in new_branches:
        print(f"  Branch: {branch}")
        print(f"  Latest commit: {commit_hash[:8]} @ {commit_date[:19]}")

    print("\nScore progression from latest run:")
    print(get_score_delta_from_log())
    print(
        "\n→ Health check only. The autoresearch loop creates PRs automatically when score delta >= threshold."
    )
    print(
        "  If score is stalled/regressing, create a task to investigate — do not manually surface PRs."
    )

    # Update review timestamp so we don't re-alert next session for same commits
    update_review_timestamp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
