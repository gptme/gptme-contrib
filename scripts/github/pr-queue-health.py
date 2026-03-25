#!/usr/bin/env python3
"""PR Queue Health Monitor.

Monitors open PRs across repositories to surface backlog health,
stale PRs, and review bottlenecks. Designed to integrate with
context generation for autonomous agent runs.

Configuration via environment variables:
  GPTME_TRACKED_REPOS   Comma-separated list of repos to scan (e.g. "owner/repo1,owner/repo2")
  GPTME_PR_LIMITS       JSON dict of per-repo limits (e.g. '{"owner/repo": 3}')

Usage:
    python3 scripts/github/pr-queue-health.py           # Summary
    python3 scripts/github/pr-queue-health.py --detail   # Per-PR details
    python3 scripts/github/pr-queue-health.py --json     # Machine-readable
    python3 scripts/github/pr-queue-health.py --context  # For context injection
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

# Default repos to scan — override via GPTME_TRACKED_REPOS env var
DEFAULT_TRACKED_REPOS = [
    "gptme/gptme",
    "gptme/gptme-contrib",
    "gptme/gptme-agent-template",
]


def get_tracked_repos() -> list[str]:
    """Get repos to scan, from env var or defaults."""
    env = os.environ.get("GPTME_TRACKED_REPOS", "")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return DEFAULT_TRACKED_REPOS


# Default per-repo open PR limits — prevents flooding any single project
DEFAULT_PER_REPO_LIMITS: dict[str, int] = {
    "gptme/gptme": 4,
    "gptme/gptme-contrib": 3,
    "gptme/gptme-agent-template": 2,
}
DEFAULT_PER_REPO_LIMIT = 2  # Fallback limit for repos not in DEFAULT_PER_REPO_LIMITS
PR_LIST_LIMIT = 100


def warn(message: str) -> None:
    """Emit a warning to stderr without aborting the run."""
    print(f"warn: {message}", file=sys.stderr)


def get_per_repo_limits() -> dict[str, int]:
    """Get per-repo PR limits, from env var (JSON) merged with defaults."""
    limits = dict(DEFAULT_PER_REPO_LIMITS)
    env = os.environ.get("GPTME_PR_LIMITS", "")
    if env:
        try:
            overrides = json.loads(env)
        except json.JSONDecodeError as exc:
            warn(f"invalid GPTME_PR_LIMITS JSON: {exc}")
        else:
            if not isinstance(overrides, dict):
                warn("GPTME_PR_LIMITS must be a JSON object mapping repo to limit")
            else:
                for repo, limit in overrides.items():
                    try:
                        limits[str(repo)] = int(limit)
                    except (TypeError, ValueError):
                        warn(
                            f"ignoring non-integer GPTME_PR_LIMITS entry for {repo!r}: {limit!r}"
                        )
    return limits


# Thresholds
PR_COUNT_GREEN = 15
PR_COUNT_YELLOW = 30
PR_STALE_DAYS = 7
PR_ANCIENT_DAYS = 14


def run_gh(args: list[str]) -> str | None:
    """Run a gh CLI command and return stdout."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        warn(f"gh command timed out: gh {' '.join(args)}")
        return None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            warn(f"gh {' '.join(args)} failed: {stderr}")
        else:
            warn(f"gh {' '.join(args)} failed with exit code {result.returncode}")
        return None
    return result.stdout.strip()


def get_gh_user() -> str:
    """Get the authenticated GitHub username."""
    return run_gh(["api", "user", "-q", ".login"]) or ""


def fetch_prs_for_repo(repo: str, author: str) -> list[dict[str, Any]] | None:
    """Fetch open PRs for a specific repo by author."""
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
            "--limit",
            str(PR_LIST_LIMIT),
            "--json",
            "number,title,createdAt,updatedAt,reviewDecision,statusCheckRollup,headRefName,url",
        ]
    )
    if raw is None:
        return None
    if not raw:
        warn(f"empty response from gh pr list for {repo}")
        return None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        warn(f"invalid JSON from gh pr list for {repo}: {exc}")
        return None
    if not isinstance(result, list):
        warn(f"unexpected gh pr list payload for {repo}: expected list")
        return None
    return result


def parse_datetime(dt_str: str) -> datetime | None:
    """Parse GitHub datetime string."""
    if not dt_str:
        return None
    for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"]:
        try:
            dt = datetime.strptime(dt_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def get_ci_status(pr: dict[str, Any]) -> str:
    """Extract CI status from PR check rollup."""
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "none"

    states = [c.get("conclusion") or c.get("status", "unknown") for c in checks]
    if any(s == "FAILURE" for s in states):
        return "failing"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED") for s in states):
        return "pending"
    if all(s == "SUCCESS" for s in states):
        return "passing"
    return "mixed"


def get_review_status(pr: dict[str, Any]) -> str:
    """Extract review decision."""
    decision = pr.get("reviewDecision") or ""
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "changes_requested"
    if decision == "REVIEW_REQUIRED":
        return "review_needed"
    return "no_reviews"


def classify_age(days: float) -> str:
    """Classify PR age."""
    if days < 3:
        return "fresh"
    if days < PR_STALE_DAYS:
        return "aging"
    if days < PR_ANCIENT_DAYS:
        return "stale"
    return "ancient"


def format_age(days: float | None) -> str:
    """Human-readable age string."""
    if days is None:
        return "n/a"
    if days < 1:
        hours = int(days * 24)
        return f"{hours}h" if hours > 0 else "<1h"
    return f"{days:.0f}d"


def compute_per_repo_violations(
    repo_counts: dict[str, int],
    per_repo_limits: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute repos over or at their per-repo PR limit.

    Returns (repos_over_limit, repos_at_limit) where each entry is
    {"repo": str, "count": int, "limit": int}.
    """
    over: list[dict[str, Any]] = []
    at: list[dict[str, Any]] = []
    for repo, count in repo_counts.items():
        if count == 0:
            continue
        limit = per_repo_limits.get(repo, DEFAULT_PER_REPO_LIMIT)
        if count > limit:
            over.append({"repo": repo, "count": count, "limit": limit})
        elif count == limit:
            at.append({"repo": repo, "count": count, "limit": limit})
    over.sort(key=lambda x: x["count"] - x["limit"], reverse=True)
    at.sort(key=lambda x: x["count"], reverse=True)
    return over, at


def health_level(
    total_prs: int, repos_over_limit: list[dict[str, Any]] | None = None
) -> str:
    """Determine overall queue health."""
    if total_prs > PR_COUNT_YELLOW:
        return "red"
    if total_prs > PR_COUNT_GREEN:
        return "yellow"
    if repos_over_limit:
        return "yellow"
    return "green"


def health_emoji(level: str) -> str:
    """Emoji for health level."""
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(level, "⚪")


def main() -> int:
    args = set(sys.argv[1:])
    detail = "--detail" in args
    as_json = "--json" in args
    as_context = "--context" in args

    user = get_gh_user()
    if not user:
        print(
            "Error: could not determine GitHub user (is gh CLI authenticated?)",
            file=sys.stderr,
        )
        return 1

    tracked_repos = get_tracked_repos()
    per_repo_limits = get_per_repo_limits()
    now = datetime.now(timezone.utc)

    all_prs: list[dict[str, Any]] = []
    repo_counts: dict[str, int] = {}
    fetch_failures: list[str] = []
    invalid_timestamp_prs = 0

    for repo in tracked_repos:
        prs = fetch_prs_for_repo(repo, user)
        if prs is None:
            fetch_failures.append(repo)
            repo_counts[repo] = 0
            continue
        repo_counts[repo] = len(prs)
        for pr in prs:
            created = parse_datetime(pr.get("createdAt", ""))
            updated = parse_datetime(pr.get("updatedAt", ""))
            if created is None or updated is None:
                invalid_timestamp_prs += 1
                warn(
                    f"skipping {repo}#{pr.get('number', '?')} due to invalid createdAt/updatedAt timestamp"
                )
                continue
            age_days = (now - created).total_seconds() / 86400
            idle_days = (now - updated).total_seconds() / 86400

            all_prs.append(
                {
                    "repo": repo,
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr.get("url", ""),
                    "branch": pr.get("headRefName", ""),
                    "created": pr.get("createdAt", ""),
                    "updated": pr.get("updatedAt", ""),
                    "age_days": round(age_days, 1),
                    "idle_days": round(idle_days, 1),
                    "age_class": classify_age(age_days),
                    "ci_status": get_ci_status(pr),
                    "review_status": get_review_status(pr),
                }
            )

    if fetch_failures and len(fetch_failures) == len(tracked_repos):
        print(
            "Error: failed to fetch PRs for all tracked repositories",
            file=sys.stderr,
        )
        return 2
    if fetch_failures:
        warn(
            "partial data: failed to fetch PRs for " + ", ".join(sorted(fetch_failures))
        )
    if invalid_timestamp_prs:
        warn(
            f"skipped {invalid_timestamp_prs} PRs with invalid timestamps when computing age-based metrics"
        )

    all_prs.sort(key=lambda p: p["age_days"], reverse=True)

    total = sum(repo_counts.values())
    valid_age_count = len(all_prs)
    repos_over, repos_at = compute_per_repo_violations(repo_counts, per_repo_limits)
    level = health_level(total, repos_over)
    avg_age = (
        sum(p["age_days"] for p in all_prs) / valid_age_count
        if valid_age_count
        else None
    )
    stale_count = sum(1 for p in all_prs if p["age_class"] in ("stale", "ancient"))
    failing_ci = sum(1 for p in all_prs if p["ci_status"] == "failing")
    needs_changes = sum(1 for p in all_prs if p["review_status"] == "changes_requested")
    approved = sum(1 for p in all_prs if p["review_status"] == "approved")
    auto_mergeable = sum(
        1
        for p in all_prs
        if p["ci_status"] == "passing" and p["review_status"] == "approved"
    )

    if as_json:
        output = {
            "total": total,
            "health": level,
            "avg_age_days": round(avg_age, 1) if avg_age is not None else None,
            "stale": stale_count,
            "failing_ci": failing_ci,
            "needs_changes": needs_changes,
            "approved": approved,
            "auto_mergeable": auto_mergeable,
            "by_repo": {k: v for k, v in repo_counts.items() if v > 0},
            "repos_over_limit": repos_over,
            "repos_at_limit": repos_at,
            "fetch_failures": fetch_failures,
            "skipped_invalid_timestamps": invalid_timestamp_prs,
            "prs": all_prs,
        }
        print(json.dumps(output, indent=2))
        return 0

    if as_context:
        print("## PR Queue Health")
        print()
        emoji = health_emoji(level)
        print(
            f"{emoji} **{total} open PRs** (target: <{PR_COUNT_YELLOW}) | avg age: {format_age(avg_age)} | stale: {stale_count}"
        )
        if failing_ci > 0:
            print(f"  ⚠️  {failing_ci} PRs with failing CI")
        if needs_changes > 0:
            print(f"  📝 {needs_changes} PRs need changes")
        if auto_mergeable > 0:
            print(f"  ✅ {auto_mergeable} PRs ready to merge (approved + CI green)")
        if total > PR_COUNT_YELLOW:
            print("  🛑 PR queue overloaded — avoid creating new PRs")
        elif total > PR_COUNT_GREEN:
            print("  ⚠️  PR queue elevated — minimize new PRs")
        print()

        if repos_over:
            for v in repos_over:
                print(
                    f"  🛑 {v['repo']}: {v['count']}/{v['limit']} PRs — over limit, do not create new PRs"
                )
        if repos_at:
            for v in repos_at:
                print(f"  ⚠️  {v['repo']}: {v['count']}/{v['limit']} PRs — at limit")
        if repos_over or repos_at:
            print()

        active_repos = {k: v for k, v in repo_counts.items() if v > 0}
        if active_repos:
            for repo, count in sorted(active_repos.items(), key=lambda x: -x[1]):
                limit = per_repo_limits.get(repo, DEFAULT_PER_REPO_LIMIT)
                print(f"  {repo}: {count}/{limit}")
        return 0

    # Default: summary view
    emoji = health_emoji(level)
    print(f"PR Queue Health: {emoji} {level.upper()}")
    print(f"{'=' * 40}")
    print(f"Total open PRs:  {total} (target: <{PR_COUNT_YELLOW})")
    print(f"Average age:     {format_age(avg_age)}")
    print(f"Stale (>{PR_STALE_DAYS}d):     {stale_count}")
    print(f"Failing CI:      {failing_ci}")
    print(f"Changes needed:  {needs_changes}")
    print(f"Approved:        {approved}")
    print(f"Auto-mergeable:  {auto_mergeable}")
    print()

    active_repos = {k: v for k, v in repo_counts.items() if v > 0}
    if active_repos:
        print("By Repository:")
        for repo, count in sorted(active_repos.items(), key=lambda x: -x[1]):
            limit = per_repo_limits.get(repo, DEFAULT_PER_REPO_LIMIT)
            marker = ""
            if count > limit:
                marker = " 🛑 OVER LIMIT"
            elif count == limit:
                marker = " ⚠️  AT LIMIT"
            print(f"  {repo}: {count}/{limit}{marker}")
        print()

    if detail:
        print("PR Details (oldest first):")
        print(f"{'-' * 80}")
        for pr in all_prs:
            age_marker = {"fresh": "🟢", "aging": "🟡", "stale": "🟠", "ancient": "🔴"}
            ci_marker = {
                "passing": "✅",
                "failing": "❌",
                "pending": "⏳",
                "none": "⚪",
                "mixed": "🔶",
            }
            review_marker = {
                "approved": "✅",
                "changes_requested": "📝",
                "review_needed": "👀",
                "no_reviews": "⚪",
            }

            print(
                f"  {age_marker.get(pr['age_class'], '⚪')} {pr['repo']}#{pr['number']}: {pr['title']}"
            )
            print(
                f"    Age: {format_age(pr['age_days'])} | CI: {ci_marker.get(pr['ci_status'], '?')} | Review: {review_marker.get(pr['review_status'], '?')} | Idle: {format_age(pr['idle_days'])}"
            )
        print()

    recommendations = []
    if auto_mergeable > 0:
        mergeable = [
            p
            for p in all_prs
            if p["ci_status"] == "passing" and p["review_status"] == "approved"
        ]
        for p in mergeable:
            recommendations.append(
                f"Merge {p['repo']}#{p['number']} (approved, CI green)"
            )
    if failing_ci > 0:
        failing = [p for p in all_prs if p["ci_status"] == "failing"]
        for p in failing:
            recommendations.append(f"Fix CI on {p['repo']}#{p['number']}")
    if needs_changes > 0:
        changes = [p for p in all_prs if p["review_status"] == "changes_requested"]
        for p in changes:
            recommendations.append(f"Address review on {p['repo']}#{p['number']}")
    if stale_count > 0:
        stale = [p for p in all_prs if p["age_class"] in ("stale", "ancient")]
        for p in stale:
            recommendations.append(
                f"Review stale PR {p['repo']}#{p['number']} ({format_age(p['age_days'])} old)"
            )
    if total > PR_COUNT_YELLOW:
        recommendations.insert(
            0,
            f"⚡ PR queue overloaded — do NOT create new PRs until backlog < {PR_COUNT_YELLOW}",
        )
    for v in repos_over:
        recommendations.append(
            f"🛑 {v['repo']} has {v['count']}/{v['limit']} open PRs — do not create new PRs for this repo"
        )
    for v in repos_at:
        recommendations.append(
            f"⚠️  {v['repo']} has {v['count']}/{v['limit']} open PRs — at limit, avoid new PRs for this repo"
        )

    if recommendations:
        print("Recommendations:")
        for rec in recommendations:
            print(f"  • {rec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
