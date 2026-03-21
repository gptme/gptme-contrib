#!/usr/bin/env python3
"""PR Greptile Review Trigger — batch-trigger safe Greptile review requests.

Scans open PRs authored by the authenticated user, identifies which ones are
actionable (`needs-re-review`), and routes triggers through greptile-helper.sh.
Designed to pre-populate re-reviews without reintroducing review-spam regressions.

Only triggers re-reviews on repos where Greptile is likely installed. Initial
reviews are auto-triggered by Greptile when a PR is opened — this script handles
re-reviews after new commits land on a previously-reviewed PR.

Usage:
    python3 scripts/github/pr-greptile-trigger.py              # Dry-run (show what would trigger)
    python3 scripts/github/pr-greptile-trigger.py --execute     # Actually trigger re-reviews
    python3 scripts/github/pr-greptile-trigger.py --status      # Show Greptile review status
    python3 scripts/github/pr-greptile-trigger.py --repo gptme/gptme  # Filter to one repo

Environment:
    GREPTILE_REPOS  Comma-separated list of repos to scan. Overrides the built-in
                    defaults when set. Example:
                    GREPTILE_REPOS=gptme/gptme,gptme/gptme-contrib python3 ...

Exit codes:
    0  All actionable re-reviews triggered successfully (or nothing to do).
    1  Partial failure — at least one trigger succeeded, at least one failed.
    2  Total failure — all trigger attempts failed, or unrecoverable error
       (auth failure, helper unavailable).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default repos where Greptile is likely installed (gptme ecosystem).
# Override with GREPTILE_REPOS env var or --repo CLI flag.
DEFAULT_GREPTILE_REPOS = [
    "gptme/gptme",
    "gptme/gptme-contrib",
    "gptme/gptme-agent-template",
    "gptme/gptme-cloud",
]

# `needs-re-review` is the primary actionable state; `stale` and `none` are
# kept for backward-compat but should not appear after PR #497 in gptme-contrib
# (March 2026).  greptile-helper.sh never triggers initial reviews —
# unreviewed PRs return `awaiting-initial-review`, not `none`.
ACTIONABLE_STATES = {"none", "stale", "needs-re-review"}

SAFE_HELPER = Path(__file__).with_name("greptile-helper.sh")


@dataclass
class PRInfo:
    repo: str
    number: int
    title: str
    url: str
    review_state: str


def run_gh(args: list[str], timeout: int = 30) -> str:
    """Run a gh CLI command and return stdout."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_gh_user() -> str:
    """Get the authenticated GitHub username."""
    return run_gh(["api", "user", "-q", ".login"]) or ""


def fetch_prs(repo: str, author: str) -> list[dict[str, Any]]:
    """Fetch open PRs for a repo authored by *author*."""
    raw = run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--author",
            author,
            "--state",
            "open",
            "--json",
            "number,title,url",
        ]
    )
    if not raw:
        print(
            f"  [warn] No PRs returned for {repo} (access error or none open)",
            file=sys.stderr,
        )
        return []
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
        for p in prs:
            p["repo"] = repo
        return prs
    except json.JSONDecodeError as e:
        print(
            f"[warn] Failed to parse JSON from `gh pr list` for {repo}: {e}",
            file=sys.stderr,
        )
        return []


def _helper_cmd(
    command: str, repo: str, pr_number: int
) -> subprocess.CompletedProcess[str] | None:
    """Run greptile-helper.sh.

    Returns None on timeout so a single hung helper invocation doesn't abort the
    whole batch.
    """
    try:
        return subprocess.run(
            ["bash", str(SAFE_HELPER), command, repo, str(pr_number)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def review_state_for_pr(repo: str, pr_number: int) -> str:
    """Return the helper's review state for a PR."""
    if not SAFE_HELPER.exists():
        return "error"
    result = _helper_cmd("status", repo, pr_number)
    if result is None or result.returncode != 0:
        return "error"
    return result.stdout.strip() or "error"


def trigger_greptile(repo: str, pr_number: int) -> tuple[bool, str]:
    """Trigger Greptile re-review through the safe helper."""
    if not SAFE_HELPER.exists():
        return False, "helper-missing"
    result = _helper_cmd("trigger", repo, pr_number)
    if result is None:
        return False, "helper-timeout"
    output = result.stdout.strip()
    return result.returncode == 0, output


def resolve_repos(repo_arg: str | None) -> list[str]:
    """Return the list of repos to scan, in priority order."""
    if repo_arg:
        return [repo_arg]
    env_repos = os.environ.get("GREPTILE_REPOS", "").strip()
    if env_repos:
        return [r.strip() for r in env_repos.split(",") if r.strip()]
    return list(DEFAULT_GREPTILE_REPOS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually trigger re-reviews (default is dry-run)",
    )
    parser.add_argument(
        "--status", action="store_true", help="Show Greptile review status for all PRs"
    )
    parser.add_argument("--repo", help="Filter to a specific repo (owner/name)")
    parser.add_argument(
        "--author",
        help="GitHub username to filter PRs by (default: authenticated user)",
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    author = args.author or get_gh_user()
    if not author:
        print(
            "Error: could not determine GitHub user. Is gh CLI authenticated?",
            file=sys.stderr,
        )
        return 2

    repos = resolve_repos(args.repo)

    # Fetch all open PRs
    all_prs: list[PRInfo] = []
    for repo in repos:
        prs = fetch_prs(repo, author)
        for raw_pr in prs:
            review_state = review_state_for_pr(repo, int(raw_pr["number"]))
            all_prs.append(
                PRInfo(
                    repo=repo,
                    number=int(raw_pr["number"]),
                    title=str(raw_pr["title"]),
                    url=str(raw_pr["url"]),
                    review_state=review_state,
                )
            )

    if not all_prs:
        print("No open PRs found in Greptile-enabled repos.")
        return 0

    # Categorize
    reviewed = [p for p in all_prs if p.review_state == "already-reviewed"]
    actionable = [p for p in all_prs if p.review_state in ACTIONABLE_STATES]
    in_progress = [p for p in all_prs if p.review_state == "in-progress"]
    awaiting = [p for p in all_prs if p.review_state == "awaiting-initial-review"]
    errors = [p for p in all_prs if p.review_state == "error"]

    if args.status:
        if args.execute:
            print("[warn] --execute is ignored when --status is set", file=sys.stderr)
        print(f"Greptile Review Status — {len(all_prs)} PRs\n")
        print(f"  ✅ Reviewed: {len(reviewed)}")
        print(f"  🔄 Actionable: {len(actionable)}")
        print(f"  ⏳ In progress: {len(in_progress)}")
        print(f"  🆕 Awaiting initial: {len(awaiting)}")
        print(f"  ❌ Errors: {len(errors)}")
        print()
        for pr in all_prs:
            state = pr.review_state
            if state == "already-reviewed":
                status = "✅ already reviewed"
            elif state == "needs-re-review":
                status = "🔄 needs re-review"
            elif state == "in-progress":
                status = "⏳ trigger in progress"
            elif state == "awaiting-initial-review":
                status = "⏳ awaiting Greptile auto-review"
            elif state == "stale":
                status = "⚠️ stale trigger"
            elif state == "none":
                status = "⬜ no review (legacy)"
            else:
                status = "❌ helper error"
            print(f"  {pr.repo}#{pr.number}: {status} — {pr.title[:60]}")
        return 0

    if not actionable:
        if errors:
            print(
                f"[warn] Could not determine Greptile status for {len(errors)} PR(s) "
                "(helper missing or errored).",
                file=sys.stderr,
            )
            if not reviewed and not in_progress and not awaiting:
                return 2
        if awaiting:
            print(
                f"All PRs are either already reviewed, in-flight, or awaiting Greptile "
                f"auto-review ({len(awaiting)} awaiting). Nothing to trigger manually."
            )
        else:
            print(
                "All PRs are already reviewed or currently in-flight. Nothing to trigger."
            )
        return 0

    print(f"PRs needing Greptile re-review: {len(actionable)}\n")
    for pr in actionable:
        label = {"none": "⬜", "stale": "⚠️", "needs-re-review": "🔄"}.get(
            pr.review_state, "•"
        )
        print(f"  {label} {pr.repo}#{pr.number}: {pr.title[:60]}")

    if not args.execute:
        print(f"\nDry run — pass --execute to trigger {len(actionable)} re-reviews.")
        return 0

    # Trigger re-reviews
    print(f"\nTriggering {len(actionable)} Greptile re-reviews...\n")
    triggered = 0
    for i, pr in enumerate(actionable):
        print(f"  Triggering {pr.repo}#{pr.number}...", end=" ", flush=True)
        success, detail = trigger_greptile(pr.repo, pr.number)
        if success:
            print("✅")
            triggered += 1
        else:
            print("❌")
        if detail:
            print(f"    {detail}")
        # Small delay to avoid rate limiting (skip after last item)
        if i < len(actionable) - 1:
            time.sleep(1)

    print(f"\nDone: {triggered}/{len(actionable)} re-reviews triggered.")
    if triggered == 0:
        return 2  # All failed
    if triggered < len(actionable):
        return 1  # Partial failure — some triggers succeeded, some failed
    return 0  # All succeeded


def main() -> int:
    return _run(_parse_args())


if __name__ == "__main__":
    sys.exit(main())
