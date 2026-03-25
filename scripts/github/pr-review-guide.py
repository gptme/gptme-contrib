#!/usr/bin/env python3
"""PR Review Guide — Difficulty estimator and prioritization tool.

Ranks open PRs by estimated review effort, helping reviewers prioritize
the queue efficiently. Produces a human-readable guide showing which PRs
are quickest to review and why.

This directly addresses the review bottleneck: when a reviewer has limited
time, they should start with the easiest PRs to maximize merge throughput.

Signals used for difficulty estimation:
- Lines changed (excluding lockfiles)
- Number of files changed
- File types (test/docs = easy, core logic = harder)
- CI status (green = ready, red = not ready)
- Greptile review coverage (reviewed = easier)
- PR description quality (longer = better context)
- Age (older PRs may have more context drift)

Configuration via environment variables:
  GPTME_TRACKED_REPOS   Comma-separated list of repos to scan
                         (default: gptme/* repos)

Usage:
    python3 scripts/github/pr-review-guide.py              # Ranked guide
    python3 scripts/github/pr-review-guide.py --json        # Machine-readable
    python3 scripts/github/pr-review-guide.py --context     # Compact for context injection
    python3 scripts/github/pr-review-guide.py --repo gptme/gptme  # Filter to repo
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Default repos to scan — gptme org only (override via GPTME_TRACKED_REPOS)
_DEFAULT_TRACKED_REPOS = [
    "gptme/gptme",
    "gptme/gptme-contrib",
    "gptme/gptme-cloud",
    "gptme/gptme-agent-template",
]

# File patterns for categorization
TEST_PATTERNS = re.compile(r"test[s_]|_test\.|\.test\.", re.IGNORECASE)
DOC_PATTERNS = re.compile(
    r"\.(md|rst|txt|adoc)$|README|CHANGELOG|LICENSE|docs/", re.IGNORECASE
)
CONFIG_PATTERNS = re.compile(
    r"\.(toml|yaml|yml|json|cfg|ini|conf)$|Makefile|\.github/|Dockerfile",
    re.IGNORECASE,
)
LOCKFILE_PATTERNS = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
}


def get_tracked_repos() -> list[str]:
    """Return repos to scan, from env var or defaults."""
    env = os.environ.get("GPTME_TRACKED_REPOS", "")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return _DEFAULT_TRACKED_REPOS


@dataclass
class ReviewEstimate:
    """Estimated review difficulty for a single PR."""

    repo: str
    number: int
    title: str
    url: str
    difficulty_score: float = 0.0  # 0-100, lower = easier
    estimated_minutes: float = 0.0
    category: str = "normal"  # quick, normal, deep, heavy
    factors: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    ci_green: bool = True
    has_conflicts: bool = False
    has_tests: bool = False
    has_greptile: bool = False
    greptile_clean: bool = True
    loc_changed: int = 0
    files_changed: int = 0
    age_days: int = 0
    file_breakdown: dict[str, int] = field(default_factory=dict)


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
    user = run_gh(["api", "user", "-q", ".login"])
    if not user:
        print("Warning: could not determine GitHub user", file=sys.stderr)
    return user


def fetch_prs(repo: str, author: str) -> list[dict[str, Any]]:
    """Fetch open PRs with review-relevant fields."""
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
            "number,title,additions,deletions,changedFiles,files,"
            "createdAt,body,statusCheckRollup,url,commits,mergeable",
        ]
    )
    if not raw:
        return []
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
        for pr in prs:
            pr["repo"] = repo
        return prs
    except json.JSONDecodeError:
        return []


def fetch_greptile_status(repo: str, pr_number: int) -> dict[str, Any]:
    """Check if Greptile has reviewed this PR and count unresolved findings."""
    owner, name = repo.split("/", 1)
    query = f"""
    {{
      repository(owner:"{owner}", name:"{name}") {{
        pullRequest(number:{pr_number}) {{
          reviewThreads(first:50) {{
            nodes {{
              isResolved
              comments(first:1) {{
                nodes {{
                  author {{ login }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """

    raw = run_gh(["api", "graphql", "-f", f"query={query}"], timeout=15)
    if not raw:
        return {"has_review": False, "unresolved": 0, "total": 0}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"has_review": False, "unresolved": 0, "total": 0}

    threads = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    total = 0
    unresolved = 0
    for thread in threads:
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = comments[0].get("author", {}).get("login", "")
        if "greptile" not in author:
            continue
        total += 1
        if not thread.get("isResolved", False):
            unresolved += 1

    return {"has_review": total > 0, "unresolved": unresolved, "total": total}


def classify_files(
    files: list[dict[str, Any]],
) -> dict[str, int]:
    """Classify changed files into categories."""
    breakdown: dict[str, int] = {"test": 0, "docs": 0, "config": 0, "logic": 0}

    for f in files:
        path = f.get("path", "")
        fname = path.split("/")[-1] if "/" in path else path

        if fname in LOCKFILE_PATTERNS:
            continue  # Skip lockfiles entirely
        elif TEST_PATTERNS.search(path):
            breakdown["test"] += 1
        elif DOC_PATTERNS.search(path):
            breakdown["docs"] += 1
        elif CONFIG_PATTERNS.search(path):
            breakdown["config"] += 1
        else:
            breakdown["logic"] += 1

    return breakdown


def compute_loc_excluding_lockfiles(pr: dict[str, Any]) -> int:
    """Compute lines changed excluding lockfile noise."""
    files = pr.get("files", [])
    if not isinstance(files, list) or not files:
        return int(pr.get("additions", 0)) + int(pr.get("deletions", 0))

    total = 0
    for f in files:
        fname = f.get("path", "").split("/")[-1]
        if fname in LOCKFILE_PATTERNS:
            continue
        total += f.get("additions", 0) + f.get("deletions", 0)
    return total


def estimate_review(
    pr: dict[str, Any], *, fetch_greptile: bool = True
) -> ReviewEstimate:
    """Estimate review difficulty for a single PR."""
    repo = pr.get("repo", "unknown")
    number = pr.get("number", 0)

    est = ReviewEstimate(
        repo=repo,
        number=number,
        title=pr.get("title", ""),
        url=pr.get("url", ""),
    )

    # --- Lines changed ---
    est.loc_changed = compute_loc_excluding_lockfiles(pr)

    # --- Files changed ---
    files = pr.get("files", [])
    if isinstance(files, list):
        # Exclude lockfiles from file count too
        non_lock = [
            f
            for f in files
            if f.get("path", "").split("/")[-1] not in LOCKFILE_PATTERNS
        ]
        est.files_changed = len(non_lock)
        est.file_breakdown = classify_files(files)
    else:
        est.files_changed = pr.get("changedFiles", 0)

    # --- Merge conflicts ---
    mergeable = pr.get("mergeable", "UNKNOWN")
    est.has_conflicts = mergeable == "CONFLICTING"

    # --- Test coverage in diff ---
    est.has_tests = est.file_breakdown.get("test", 0) > 0

    # --- CI status ---
    checks = pr.get("statusCheckRollup", [])
    if isinstance(checks, list):
        failed = [c for c in checks if c.get("conclusion") == "FAILURE"]
        est.ci_green = len(failed) == 0
    else:
        est.ci_green = True  # Assume green if no data

    # --- PR age ---
    created_str = pr.get("createdAt", "")
    if created_str:
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            est.age_days = (datetime.now(timezone.utc) - created).days
        except (ValueError, TypeError):
            pass

    # --- Description quality ---
    body = pr.get("body", "") or ""
    body_length = len(body.strip())

    # --- Greptile review status ---
    if fetch_greptile:
        greptile = fetch_greptile_status(repo, number)
        est.has_greptile = greptile["has_review"]
        est.greptile_clean = greptile["unresolved"] == 0
    else:
        est.has_greptile = False
        est.greptile_clean = True

    # --- Compute difficulty score (0-100) ---
    score = 0.0
    fb = est.file_breakdown

    # Base score from LOC
    if est.loc_changed <= 20:
        score += 5
        est.positives.append("tiny change")
    elif est.loc_changed <= 50:
        score += 10
    elif est.loc_changed <= 150:
        score += 25
    elif est.loc_changed <= 300:
        score += 40
    elif est.loc_changed <= 600:
        score += 60
        est.factors.append(f"{est.loc_changed} LOC")
    else:
        score += 80
        est.factors.append(f"{est.loc_changed} LOC (very large)")

    # File count factor
    if est.files_changed <= 2:
        est.positives.append("few files")
    elif est.files_changed <= 5:
        score += 5
    elif est.files_changed <= 10:
        score += 10
        est.factors.append(f"{est.files_changed} files")
    else:
        score += 20
        est.factors.append(f"{est.files_changed} files (many)")

    # File type bonuses/penalties
    total_categorized = sum(fb.values()) if fb else est.files_changed
    if total_categorized > 0:
        logic_ratio = fb.get("logic", 0) / total_categorized
        test_ratio = fb.get("test", 0) / total_categorized
        docs_ratio = fb.get("docs", 0) / total_categorized

        if docs_ratio >= 0.8:
            score *= 0.3  # Docs-heavy = much easier
            est.positives.append("docs-only")
        elif test_ratio >= 0.5:
            score *= 0.5  # Test-heavy = easier
            est.positives.append("mostly tests")
        elif logic_ratio >= 0.8 and est.loc_changed > 100:
            score *= 1.3  # Logic-heavy + large = harder
            est.factors.append("logic-heavy")

    # Merge conflicts
    if est.has_conflicts:
        score += 20
        est.factors.append("merge conflicts")

    # Test coverage signal
    if est.has_tests and est.loc_changed > 50:
        score *= 0.85  # Has tests = easier to verify
        est.positives.append("includes tests")
    elif not est.has_tests and fb.get("logic", 0) > 0 and est.loc_changed > 100:
        score += 5
        est.factors.append("no tests for logic changes")

    # CI status
    if not est.ci_green:
        score += 15
        est.factors.append("CI failing")

    # Greptile coverage
    if est.has_greptile and est.greptile_clean:
        score *= 0.8  # Greptile reviewed and clean = confidence boost
        est.positives.append("Greptile clean")
    elif est.has_greptile and not est.greptile_clean:
        score += 10
        est.factors.append("unresolved Greptile findings")

    # Description quality
    if body_length > 200:
        score *= 0.9  # Good description helps
        est.positives.append("good description")
    elif body_length < 50:
        score += 5
        est.factors.append("sparse description")

    # Age factor (older PRs may have context drift)
    if est.age_days > 14:
        score += 5
        est.factors.append(f"{est.age_days}d old")
    elif est.age_days > 7:
        score += 2

    # Clamp to 0-100
    est.difficulty_score = max(0.0, min(100.0, round(score, 1)))

    # Estimate review time in minutes
    if est.difficulty_score <= 18:
        est.estimated_minutes = 2
        est.category = "quick"
    elif est.difficulty_score <= 38:
        est.estimated_minutes = 5
        est.category = "normal"
    elif est.difficulty_score <= 60:
        est.estimated_minutes = 15
        est.category = "deep"
    else:
        est.estimated_minutes = 25
        est.category = "heavy"

    return est


def format_estimate(est: ReviewEstimate, rank: int) -> str:
    """Format a single estimate for display."""
    icon = {"quick": "⚡", "normal": "📋", "deep": "🔍", "heavy": "🏋️"}.get(
        est.category, "📋"
    )

    lines = [
        f"  {rank}. {icon} [{est.category.upper()}] {est.repo}#{est.number} "
        f"(~{est.estimated_minutes:.0f}min)",
        f"     {est.title}",
        f"     {est.loc_changed} LOC, {est.files_changed} files, {est.age_days}d old"
        f" | CI: {'✅' if est.ci_green else '❌'}"
        f" | Merge: {'⚠️ conflicts' if est.has_conflicts else '✅'}"
        f" | Greptile: {'✅' if est.has_greptile and est.greptile_clean else '⬜' if not est.has_greptile else '⚠️'}",
    ]

    if est.positives:
        lines.append(f"     ✓ {', '.join(est.positives)}")
    if est.factors:
        lines.append(f"     ⚠ {', '.join(est.factors)}")

    return "\n".join(lines)


def format_context(estimates: list[ReviewEstimate]) -> str:
    """Compact format for context injection."""
    lines = ["PR Review Guide (easiest first):"]
    for i, est in enumerate(estimates, 1):
        icon = {"quick": "⚡", "normal": "📋", "deep": "🔍", "heavy": "🏋️"}.get(
            est.category, "📋"
        )
        ci = "✅" if est.ci_green else "❌"
        conflict = " ⚠️CONFLICT" if est.has_conflicts else ""
        lines.append(
            f"  {i}. {icon} {est.repo}#{est.number} ~{est.estimated_minutes:.0f}min "
            f"| {est.loc_changed}LOC {ci}{conflict} | {est.title[:50]}"
        )
    total_min = sum(e.estimated_minutes for e in estimates)
    quick = sum(1 for e in estimates if e.category == "quick")
    lines.append(f"  Total: ~{total_min:.0f}min ({quick} quick reviews)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PR Review Guide — difficulty estimator and prioritization"
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--context", action="store_true", help="Compact format for context injection"
    )
    parser.add_argument("--repo", help="Filter to specific repo")
    parser.add_argument(
        "--no-greptile",
        action="store_true",
        help="Skip Greptile status check (faster)",
    )
    args = parser.parse_args()

    author = get_gh_user()
    if not author:
        print("Error: could not determine GitHub user", file=sys.stderr)
        return 1

    # Determine repos to scan
    repos = [args.repo] if args.repo else get_tracked_repos()

    # Fetch all open PRs
    all_prs: list[dict[str, Any]] = []
    for repo in repos:
        prs = fetch_prs(repo, author)
        all_prs.extend(prs)

    if not all_prs:
        print("No open PRs found.")
        return 0

    # Estimate review difficulty for each PR
    estimates = [
        estimate_review(pr, fetch_greptile=not args.no_greptile) for pr in all_prs
    ]

    # Sort by difficulty (easiest first)
    estimates.sort(key=lambda e: e.difficulty_score)

    if args.json:
        output = {
            "total_prs": len(estimates),
            "total_estimated_minutes": sum(e.estimated_minutes for e in estimates),
            "by_category": {
                cat: sum(1 for e in estimates if e.category == cat)
                for cat in ["quick", "normal", "deep", "heavy"]
            },
            "estimates": [
                {
                    "rank": i + 1,
                    "repo": e.repo,
                    "number": e.number,
                    "title": e.title,
                    "url": e.url,
                    "difficulty_score": e.difficulty_score,
                    "estimated_minutes": e.estimated_minutes,
                    "category": e.category,
                    "loc_changed": e.loc_changed,
                    "files_changed": e.files_changed,
                    "age_days": e.age_days,
                    "ci_green": e.ci_green,
                    "has_conflicts": e.has_conflicts,
                    "has_tests": e.has_tests,
                    "has_greptile": e.has_greptile,
                    "greptile_clean": e.greptile_clean,
                    "file_breakdown": e.file_breakdown,
                    "factors": e.factors,
                    "positives": e.positives,
                }
                for i, e in enumerate(estimates)
            ],
        }
        print(json.dumps(output, indent=2))
        return 0

    if args.context:
        print(format_context(estimates))
        return 0

    # Full display
    total_min = sum(e.estimated_minutes for e in estimates)
    by_cat: dict[str, int] = {}
    for e in estimates:
        by_cat[e.category] = by_cat.get(e.category, 0) + 1

    print(f"PR Review Guide — {len(estimates)} PRs, ~{total_min:.0f}min total")
    print(
        f"  ⚡ Quick: {by_cat.get('quick', 0)}  📋 Normal: {by_cat.get('normal', 0)}"
        f"  🔍 Deep: {by_cat.get('deep', 0)}  🏋️ Heavy: {by_cat.get('heavy', 0)}"
    )
    print()

    # Group by category
    for category, label in [
        ("quick", "⚡ QUICK REVIEWS (start here)"),
        ("normal", "📋 NORMAL REVIEWS"),
        ("deep", "🔍 DEEP REVIEWS"),
        ("heavy", "🏋️ HEAVY REVIEWS"),
    ]:
        cat_estimates = [e for e in estimates if e.category == category]
        if not cat_estimates:
            continue

        cat_min = sum(e.estimated_minutes for e in cat_estimates)
        print(f"{'─' * 60}")
        print(f"{label} ({len(cat_estimates)} PRs, ~{cat_min:.0f}min)")
        print()

        for e in cat_estimates:
            global_rank = estimates.index(e) + 1
            print(format_estimate(e, global_rank))
            print()

    # Summary recommendation
    quick_prs = [e for e in estimates if e.category == "quick"]
    if quick_prs:
        quick_min = sum(e.estimated_minutes for e in quick_prs)
        print(f"{'─' * 60}")
        print(
            f"💡 Recommendation: Start with {len(quick_prs)} quick reviews "
            f"(~{quick_min:.0f}min total) to maximize merge throughput."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
