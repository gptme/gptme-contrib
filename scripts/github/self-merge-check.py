#!/usr/bin/env python3
"""Check whether a PR qualifies for self-merge under the agent's self-merge policy.

Evaluates a PR against a conservative set of rules that determine whether an
autonomous AI agent can safely merge its own PRs without human review.

Policy summary:
- CI must be green (or skipped)
- Greptile review must be present and have no unresolved threads
- PR must be authored by the authenticated user
- Changed files must fall into a low-risk category (tests, docs, lessons, internal
  tooling, task metadata)
- Sensitive/security/infra paths immediately disqualify the PR

The workspace repo (the repo where the agent's brain lives) is treated separately:
cross-repo PRs (agent pushing to external repos) require an explicit
`--allow-cross-repo` flag to be eligible.

Usage:
    python3 scripts/github/self-merge-check.py <pr-url>
    python3 scripts/github/self-merge-check.py --repo gptme/gptme 123
    python3 scripts/github/self-merge-check.py --json <pr-url>
    python3 scripts/github/self-merge-check.py --allow-cross-repo <pr-url>

Environment:
    WORKSPACE_REPO  Override auto-detected workspace repo (owner/name format).
                    Defaults to the repo inferred from `git remote get-url origin`
                    in the script's directory tree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
SPEC_LIKE_DOCS = {
    "README.md",
    "ABOUT.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "AGENTS.md",
    "SOCIAL.md",
    "GOALS.md",
    "GLOSSARY.md",
    "TASKS.md",
    "OVERVIEW.md",
}
TEST_MARKERS = ("tests/", "test_", "_test.", ".test.")
SENSITIVE_PATH_PREFIXES = (
    ".github/workflows/",
    "dotfiles/.config/systemd/",
    "scripts/runs/",
    "scripts/deploy",
    "infra/",
    "k8s/",
    "secrets/",
)
SENSITIVE_PATH_PARTS = (
    "secret",
    "credential",
    "token",
    "auth",
    "oauth",
    "ssh",
    "deploy",
    "systemd",
    "kube",
    "k8s",
)
INTERNAL_TOOLING_PREFIXES = (
    "scripts/",
    "packages/",
    "state/",
)
TASK_METADATA_PREFIXES = (
    "tasks/",
    "journal/",
    "state/calendars/",
)
LESSON_PREFIXES = (
    "lessons/",
    "knowledge/lessons/",
)
BOT_CONFIG_FILES = {
    ".pre-commit-config.yaml",
    "Makefile",
}
BOT_CONFIG_PREFIXES = (".github/",)


@dataclass
class CheckResult:
    eligible: bool
    repo: str
    number: int
    url: str
    title: str
    author: str
    category: str | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def run_gh(args: list[str], timeout: int = 30) -> str:
    """Run gh and return stdout, or empty string on failure."""
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
    return run_gh(["api", "user", "-q", ".login"]) or ""


def detect_workspace_repo() -> str:
    """Infer workspace repo from the git remote of the script's directory.

    Walks up from the script's location looking for a .git directory, then
    reads the origin remote URL and converts it to owner/repo format.

    Returns empty string if detection fails.
    """

    # Try to find git root from current working directory first, then script dir
    for start_dir in (Path.cwd(), Path(__file__).resolve().parent):
        candidate = start_dir
        for _ in range(10):  # max 10 levels up
            if (candidate / ".git").exists():
                try:
                    result = subprocess.run(
                        ["git", "-C", str(candidate), "remote", "get-url", "origin"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        url = result.stdout.strip()
                        return _parse_remote_url(url)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                break
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
    return ""


def _parse_remote_url(url: str) -> str:
    """Convert a git remote URL to owner/repo format.

    Handles:
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    - https://github.com/owner/repo
    """
    url = url.strip()
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return ""
    # Strip .git suffix
    if path.endswith(".git"):
        path = path[:-4]
    # Normalize to owner/repo (at most 2 components)
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def parse_pr_target(
    pr: str | None, repo: str | None, number: int | None
) -> tuple[str, int]:
    """Normalize CLI input into (repo, pr_number)."""
    if pr:
        pr = pr.strip()
        if pr.isdigit():
            if not repo:
                raise ValueError("--repo is required when PR is given as a number")
            return repo, int(pr)
        if pr.startswith("http://") or pr.startswith("https://"):
            parts = pr.rstrip("/").split("/")
            if len(parts) < 2 or parts[-2] != "pull":
                raise ValueError(f"Not a PR URL: {pr}")
            return "/".join(parts[-4:-2]), int(parts[-1])
        if "#" in pr:
            repo_part, number_part = pr.split("#", 1)
            return repo_part, int(number_part)
        raise ValueError(f"Unrecognized PR specifier: {pr}")
    if repo and number is not None:
        return repo, number
    raise ValueError("Provide a PR URL/specifier or --repo with PR number")


def fetch_pr(repo: str, number: int) -> dict[str, Any]:
    raw = run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,url,author,files,statusCheckRollup,isDraft,state,reviewDecision",
        ]
    )
    if not raw:
        raise RuntimeError(f"Failed to fetch PR {repo}#{number}")
    return cast(dict[str, Any], json.loads(raw))


def fetch_greptile_status(repo: str, pr_number: int) -> dict[str, Any]:
    """Check if Greptile reviewed this PR and count unresolved threads.

    Only considers threads from the most recent Greptile review cycle so that
    re-reviews after fixes are not blocked by old unresolved thread noise.
    """
    owner, name = repo.split("/", 1)
    query = """
    {
      repository(owner:"%s", name:"%s") {
        pullRequest(number:%d) {
          reviews(last:50) {
            nodes {
              author { login }
              submittedAt
              state
            }
          }
          reviewThreads(first:50) {
            nodes {
              isResolved
              comments(first:1) {
                nodes {
                  author { login }
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """ % (owner, name, pr_number)

    raw = run_gh(["api", "graphql", "-f", f"query={query}"], timeout=15)
    if not raw:
        return {"has_review": False, "unresolved": 0, "total": 0}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"has_review": False, "unresolved": 0, "total": 0}

    pr_data = data.get("data", {}).get("repository", {}).get("pullRequest", {})
    if not pr_data:
        return {"has_review": False, "unresolved": 0, "total": 0}

    reviews = pr_data.get("reviews", {}).get("nodes", [])
    threads = pr_data.get("reviewThreads", {}).get("nodes", [])

    greptile_reviews = [
        r
        for r in reviews
        if "greptile" in (r.get("author", {}).get("login", "") or "").lower()
    ]

    if not greptile_reviews:
        # Fall back to issue comments for summary-only reviews
        comments_raw = run_gh(
            [
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--jq",
                '[.[] | select(.user.login | test("greptile";"i"))] | length',
            ],
            timeout=15,
        )
        has_summary = (
            bool(comments_raw)
            and comments_raw.strip().isdigit()
            and int(comments_raw.strip()) > 0
        )
        return {"has_review": has_summary, "unresolved": 0, "total": 0}

    latest_review_time = max(r.get("submittedAt", "") for r in greptile_reviews)

    total = 0
    unresolved = 0
    for thread in threads:
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = comments[0].get("author", {}).get("login", "")
        if "greptile" not in author.lower():
            continue
        created_at = comments[0].get("createdAt", "")
        if created_at and created_at < latest_review_time:
            continue
        total += 1
        if not thread.get("isResolved", False):
            unresolved += 1

    return {"has_review": True, "unresolved": unresolved, "total": total}


def checks_green(status_checks: list[dict[str, Any]]) -> bool:
    """Return True if all reported checks are success/skipped/neutral."""
    allowed = {"SUCCESS", "SKIPPED", "NEUTRAL"}
    seen = False
    for check in status_checks or []:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if status and status != "COMPLETED":
            return False
        if conclusion:
            seen = True
            if conclusion not in allowed:
                return False
    return seen


def is_doc_file(path: str) -> bool:
    return Path(path).suffix.lower() in DOC_EXTENSIONS


def is_spec_like_doc(path: str) -> bool:
    return Path(path).name in SPEC_LIKE_DOCS


def is_test_file(path: str) -> bool:
    return path.startswith("tests/") or any(marker in path for marker in TEST_MARKERS)


def is_internal_tooling(path: str) -> bool:
    return path.startswith(INTERNAL_TOOLING_PREFIXES)


def is_task_metadata(path: str) -> bool:
    return path.startswith(TASK_METADATA_PREFIXES)


def is_lesson_file(path: str) -> bool:
    return path.startswith(LESSON_PREFIXES)


def is_bot_config(path: str) -> bool:
    return path in BOT_CONFIG_FILES or path.startswith(BOT_CONFIG_PREFIXES)


def is_sensitive_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    if normalized.startswith(tuple(p.lower() for p in SENSITIVE_PATH_PREFIXES)):
        return True
    components = normalized.split("/")
    return any(
        any(part in component for component in components)
        for part in SENSITIVE_PATH_PARTS
    )


def is_allowed_file(path: str) -> bool:
    """Check if a file falls into any allowed self-merge category."""
    if is_sensitive_path(path):
        return False
    if is_bot_config(path):
        return False
    if is_doc_file(path) and is_spec_like_doc(path):
        return False
    return (
        is_test_file(path)
        or is_lesson_file(path)
        or is_task_metadata(path)
        or is_internal_tooling(path)
        or is_doc_file(path)
    )


def classify_category(paths: list[str]) -> tuple[str | None, list[str]]:
    """Return the allowed category, or None with disqualifying reasons.

    The policy says all changed files must fall into *one of* the allowed
    categories. A PR mixing scripts/ and tasks/ files is still low-risk and
    eligible as "mixed-allowed".
    """
    reasons: list[str] = []
    if not paths:
        return None, ["PR has no changed files"]

    if any(is_sensitive_path(path) for path in paths):
        reasons.append("Touches sensitive/security/infra paths")
        return None, reasons

    if any(is_bot_config(path) for path in paths):
        reasons.append(
            "Bot/CI config changes require human review under current policy"
        )
        return None, reasons

    if any(is_doc_file(path) and is_spec_like_doc(path) for path in paths):
        reasons.append("Touches spec-like documentation requiring human review")
        return None, reasons

    # Single-category fast paths
    if all(is_test_file(path) for path in paths):
        return "test-only", reasons
    if all(is_lesson_file(path) for path in paths):
        return "lesson-updates", reasons
    if all(is_task_metadata(path) for path in paths):
        return "task-journal-metadata", reasons
    if all(is_internal_tooling(path) for path in paths):
        return "internal-tooling", reasons
    if all(is_doc_file(path) for path in paths):
        return "docs-only", reasons

    # Mixed-category: eligible if ALL files are in some allowed category
    if all(is_allowed_file(path) for path in paths):
        categories: list[str] = []
        if any(is_test_file(path) for path in paths):
            categories.append("tests")
        if any(is_lesson_file(path) for path in paths):
            categories.append("lessons")
        if any(is_task_metadata(path) for path in paths):
            categories.append("task-metadata")
        if any(is_internal_tooling(path) for path in paths):
            categories.append("internal-tooling")
        if any(is_doc_file(path) for path in paths):
            categories.append("docs")
        return f"mixed-allowed({'+'.join(categories)})", reasons

    return None, ["Changed files do not fit an allowed self-merge category"]


def evaluate_pr(
    repo: str, number: int, *, workspace_repo: str, allow_cross_repo: bool = False
) -> CheckResult:
    pr = fetch_pr(repo, number)
    author = pr.get("author", {}).get("login", "")
    title = pr.get("title", "")
    url = pr.get("url", f"https://github.com/{repo}/pull/{number}")
    files = [f.get("path", "") for f in pr.get("files", []) if f.get("path")]

    result = CheckResult(
        eligible=False,
        repo=repo,
        number=number,
        url=url,
        title=title,
        author=author,
        files=files,
    )

    current_user = get_gh_user()
    if current_user and author != current_user:
        result.reasons.append(f"PR author is {author}, expected {current_user}")

    if workspace_repo and repo != workspace_repo and not allow_cross_repo:
        result.reasons.append(
            f"Cross-repo PR ({repo}) is not eligible without --allow-cross-repo"
        )

    if pr.get("isDraft"):
        result.reasons.append("PR is still a draft")

    if pr.get("state") != "OPEN":
        result.reasons.append(f"PR state is {pr.get('state')}, not OPEN")

    if not checks_green(pr.get("statusCheckRollup", [])):
        result.reasons.append("CI is not fully green")

    greptile = fetch_greptile_status(repo, number)
    if not greptile["has_review"]:
        result.reasons.append("Greptile review not found")
    elif greptile["unresolved"] > 0:
        result.reasons.append(
            f"Greptile has {greptile['unresolved']} unresolved review thread(s)"
        )

    category, category_reasons = classify_category(files)
    result.category = category
    result.reasons.extend(category_reasons)

    if pr.get("reviewDecision") not in (None, "", "REVIEW_REQUIRED"):
        result.warnings.append(f"Review decision: {pr.get('reviewDecision')}")

    result.eligible = not result.reasons
    return result


def format_human(result: CheckResult) -> str:
    lines = [
        f"PR: {result.repo}#{result.number} — {result.title}",
        f"URL: {result.url}",
        f"Author: {result.author}",
        f"Eligible: {'YES' if result.eligible else 'NO'}",
        f"Category: {result.category or 'none'}",
    ]
    if result.reasons:
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in result.reasons)
    if result.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in result.warnings)
    if result.files:
        lines.append("Files:")
        lines.extend(f"- {path}" for path in result.files)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pr", nargs="?", help="PR URL, repo#num, or PR number")
    parser.add_argument(
        "number", nargs="?", type=int, help="PR number when using --repo"
    )
    parser.add_argument("--repo", help="Repository in owner/name form")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--allow-cross-repo",
        action="store_true",
        help="Allow merging PRs in repos other than the workspace repo",
    )
    parser.add_argument(
        "--workspace-repo",
        help=(
            "Workspace repo (owner/name) for cross-repo policy. "
            "Auto-detected from git remote if not provided."
        ),
    )
    args = parser.parse_args()

    # Resolve workspace repo
    workspace_repo = (
        args.workspace_repo
        or os.environ.get("WORKSPACE_REPO", "")
        or detect_workspace_repo()
    )

    try:
        repo, number = parse_pr_target(args.pr, args.repo, args.number)
        result = evaluate_pr(
            repo,
            number,
            workspace_repo=workspace_repo,
            allow_cross_repo=args.allow_cross_repo,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        if workspace_repo:
            print(f"Workspace repo: {workspace_repo}")
        print(format_human(result))

    return 0 if result.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
