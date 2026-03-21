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

The workspace repo (the repo where the agent's brain lives) is detected automatically.
Cross-repo PRs (agent pushing to external repos) are disqualified by default.
To allow cross-repo merges, clear WORKSPACE_REPO (set it to empty string).

Usage:
    python3 scripts/github/self-merge-check.py <pr-url>
    python3 scripts/github/self-merge-check.py --repo gptme/gptme 123
    python3 scripts/github/self-merge-check.py --json <pr-url>

Environment:
    WORKSPACE_REPO  Override auto-detected workspace repo (owner/name format).
                    Set to empty string to disable cross-repo restriction.
                    Defaults to the repo inferred from `git remote get-url origin`
                    in the script's directory tree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

MAX_GRAPHQL_PAGE_SIZE = 100

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
    "scripts/deploy",
    # Use prefix without extension — catches hyphen/underscore variants and versioned copies
    # e.g. self-merge-check.py, self_merge_check.py, self-merge-check-v2.py all match
    "scripts/github/self-merge-check",
    "scripts/github/pr-greptile-trigger",
    "scripts/github/greptile-helper",
    "infra/",
    "k8s/",
    "secrets/",
)
SENSITIVE_PATH_PARTS = (
    "secret",
    "credential",
    "token",
    "auth",
    "authentication",
    "authorization",
    "oauth",
    "oauth2",
    "ssh",
    "deploy",
    "deployment",
    "deployer",
    "systemd",
    "kube",
    "k8s",
)
INTERNAL_TOOLING_PREFIXES = (
    "scripts/",
    "packages/",
)
TASK_METADATA_PREFIXES = (
    "tasks/",
    "journal/",
)
LESSON_PREFIXES = ("lessons/",)
# Exact full-path match — intentionally only root-level files.
# Nested Makefiles (e.g. scripts/Makefile, packages/Makefile) fall through to
# is_internal_tooling() instead. If that scope should change, extend this set.
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
            if result.stderr:
                print(f"[gh error] {result.stderr.strip()}", file=sys.stderr)
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def get_gh_user() -> str:
    return run_gh(["api", "user", "-q", ".login"]) or ""


def detect_workspace_repo() -> str:
    """Infer workspace repo from the git remote of the current working directory.

    Tries CWD first, then the script's directory, walking up to find a .git
    directory and reads the origin remote URL to convert it to owner/repo format.

    Returns empty string if detection fails.
    """

    # Try to find git root from current working directory first, then script dir
    seen_roots: set[Path] = set()
    for start_dir in (Path.cwd(), Path(__file__).resolve().parent):
        candidate = start_dir
        for _ in range(10):  # max 10 levels up
            if (candidate / ".git").exists():
                if candidate in seen_roots:
                    break
                seen_roots.add(candidate)
                try:
                    result = subprocess.run(
                        ["git", "-C", str(candidate), "remote", "get-url", "origin"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        url = result.stdout.strip()
                        parsed = _parse_remote_url(url)
                        if parsed:
                            return parsed
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
            # Strip query string and fragment before parsing (e.g. ?tab=files, #discussion)
            clean_pr = pr.split("?")[0].split("#")[0].rstrip("/")
            parts = clean_pr.split("/")
            # Valid GitHub PR URL: https://github.com/owner/repo/pull/123 = 7 parts
            if len(parts) < 7 or parts[-2] != "pull":
                raise ValueError(f"Not a PR URL: {pr}")
            return "/".join(parts[-4:-2]), int(parts[-1])
        if "#" in pr:
            repo_part, number_part = pr.split("#", 1)
            return repo_part, int(number_part)
        raise ValueError(f"Unrecognized PR specifier: {pr}")
    if repo and number is not None:
        return repo, number
    raise ValueError("Provide a PR URL/specifier or --repo with PR number")


def _fetch_pr_files(repo: str, number: int) -> list[dict[str, Any]]:
    # Use subprocess.run directly so we can check the exit code.  A PR with 0
    # changed files produces empty stdout (jq emits nothing for an empty array),
    # which is indistinguishable from a failure when we only look at the output
    # string.  Checking returncode lets us correctly return [] for the 0-file
    # case instead of incorrectly raising RuntimeError.
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{number}/files",
                "--paginate",
                "--jq",
                ".[] | {path: .filename}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out fetching PR files for {repo}#{number} (>60 s)")
    if result.returncode != 0:
        raise RuntimeError(f"Failed to fetch PR files for {repo}#{number}")
    raw = result.stdout.strip()

    files: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        files.append(cast(dict[str, Any], json.loads(line)))
    return files


def fetch_pr(repo: str, number: int) -> dict[str, Any]:
    raw = run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,url,author,statusCheckRollup,isDraft,state,reviewDecision",
        ]
    )
    if not raw:
        raise RuntimeError(f"Failed to fetch PR metadata for {repo}#{number}")

    pr = cast(dict[str, Any], json.loads(raw))
    pr["files"] = _fetch_pr_files(repo, number)
    return pr


def _fetch_greptile_review_data(
    repo: str, pr_number: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Fetch Greptile reviews and all review threads for a PR.

    Review threads are paginated to avoid silently undercounting unresolved
    Greptile comments on large PRs.
    """
    owner, name = repo.split("/", 1)
    all_threads: list[dict[str, Any]] = []
    # None = not yet fetched; [] = fetched but no reviews found.
    # This distinction prevents re-including the reviews block on every pagination
    # page when the PR has no Greptile reviews (empty list is falsy, so `if not
    # reviews` would incorrectly re-request reviews on every subsequent page).
    reviews: list[dict[str, Any]] | None = None
    cursor: str | None = None

    while True:
        # Only fetch reviews on the first page; subsequent pages only need threads.
        reviews_block = (
            """reviews(last:50) {
                nodes {
                  author { login }
                  submittedAt
                  state
                }
              }"""
            if reviews is None
            else ""
        )
        # Use $after variable to avoid cursor string interpolation.
        after_arg = ", after: $after" if cursor else ""
        after_var_decl = ", $after: String" if cursor else ""
        query = f"""
        query($owner: String!, $name: String!, $pr: Int!{after_var_decl}) {{
          repository(owner: $owner, name: $name) {{
            pullRequest(number: $pr) {{
              {reviews_block}
              reviewThreads(first:{MAX_GRAPHQL_PAGE_SIZE}{after_arg}) {{
                pageInfo {{
                  hasNextPage
                  endCursor
                }}
                nodes {{
                  isResolved
                  comments(first:1) {{
                    nodes {{
                      author {{ login }}
                      createdAt
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        gh_args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"pr={pr_number}",
        ]
        if cursor:
            gh_args += ["-f", f"after={cursor}"]
        raw = run_gh(gh_args, timeout=15)
        if not raw:
            # If we already have review data from page 1, degrade gracefully by
            # returning what was collected so far rather than discarding it.  A
            # mid-pagination transient error should not produce a false "Greptile
            # review not found" result.
            if reviews is not None:
                break
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        pr_data = data.get("data", {}).get("repository", {}).get("pullRequest", {})
        if not pr_data:
            return None

        if reviews is None:
            reviews = (pr_data.get("reviews") or {}).get("nodes", [])

        threads_data = pr_data.get("reviewThreads", {})
        all_threads.extend(threads_data.get("nodes", []))
        page_info = threads_data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    # reviews is always a list here: all break paths require at least one successful
    # page, and reviews is set from the first page's response (line 377).  The
    # `or []` guard would only fire if reviews were None, which is unreachable at
    # this point — use an explicit None-check to make the invariant clear.
    return (reviews if reviews is not None else []), all_threads


def fetch_greptile_status(repo: str, pr_number: int) -> dict[str, Any]:
    """Check if Greptile reviewed this PR and count unresolved threads.

    Only considers threads from the most recent Greptile review cycle so that
    re-reviews after fixes are not blocked by old unresolved thread noise.
    """
    review_data = _fetch_greptile_review_data(repo, pr_number)
    if review_data is None:
        return {"has_review": False, "unresolved": 0, "total": 0}

    reviews, threads = review_data

    greptile_reviews = [
        r
        for r in reviews
        if "greptile" in ((r.get("author") or {}).get("login", "") or "").lower()
    ]

    if not greptile_reviews:
        # Fall back to issue comments for summary-only reviews.
        # Use --paginate so Greptile's comment is found even on PRs with >30 comments.
        comments_raw = run_gh(
            [
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--paginate",
                "--jq",
                '.[] | select(.user.login | test("greptile";"i")) | .id',
            ],
            timeout=30,
        )
        has_summary = bool(comments_raw and comments_raw.strip())
        return {"has_review": has_summary, "unresolved": 0, "total": 0}

    def _parse_ts(ts: str) -> datetime:
        """Parse ISO 8601 timestamp to timezone-aware datetime (handles Z and +00:00)."""
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    latest_review_time = max(
        (_parse_ts(r["submittedAt"]) for r in greptile_reviews if r.get("submittedAt")),
        default=datetime.min.replace(tzinfo=timezone.utc),
    )

    total = 0
    unresolved = 0
    for thread in threads:
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = (comments[0].get("author") or {}).get("login", "")
        if "greptile" not in author.lower():
            continue
        created_at = comments[0].get("createdAt", "")
        if created_at and _parse_ts(created_at) < latest_review_time:
            continue
        total += 1
        if not thread.get("isResolved", False):
            unresolved += 1

    return {"has_review": True, "unresolved": unresolved, "total": total}


def checks_green(status_checks: list[dict[str, Any]]) -> bool:
    """Return True if all reported checks are success/skipped/neutral.

    Returns True when no checks are configured (no CI), treating the
    absence of CI as neutral rather than failing. Individual checks with no
    status and no conclusion are treated as indeterminate, not passing.
    """
    if not status_checks:
        return True
    allowed = {"SUCCESS", "SKIPPED", "NEUTRAL"}
    for check in status_checks:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if not status and not conclusion:
            return False
        if status and status != "COMPLETED":
            return False
        if status == "COMPLETED" and not conclusion:
            return False
        if conclusion and conclusion not in allowed:
            return False
    return True


def is_doc_file(path: str) -> bool:
    return Path(path).suffix.lower() in DOC_EXTENSIONS


def is_spec_like_doc(path: str) -> bool:
    return Path(path).name in SPEC_LIKE_DOCS


def is_test_file(path: str) -> bool:
    return any(marker in path for marker in TEST_MARKERS)


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
    # Use the original (pre-lowercase) path to preserve camelCase boundaries for
    # detection; e.g. "authToken.py" → "auth_token" catches the "auth" rule.
    original_components = path.replace("\\", "/").split("/")
    for component in original_components:
        stem = component.rsplit(".", 1)[0] if "." in component else component
        # Normalise camelCase → snake_case so camelCase filenames are matched.
        # e.g. "authToken" → "auth_token", "deployScript" → "deploy_script".
        stem_normalised = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", stem).lower()
        for part in SENSITIVE_PATH_PARTS:
            # Match whole words (word boundaries: start/end or separator chars).
            # Allow trailing "s" for common plurals (token→tokens, secret→secrets).
            if re.search(
                r"(?:^|[_\-\.])" + re.escape(part) + r"s?(?:[_\-\.]|$)", stem_normalised
            ):
                return True
    return False


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


def evaluate_pr(repo: str, number: int, *, workspace_repo: str) -> CheckResult:
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
    if not current_user:
        result.reasons.append(
            "Could not determine authenticated GitHub user; author-identity check failed."
        )
    elif author != current_user:
        result.reasons.append(f"PR author is {author}, expected {current_user}")

    if not workspace_repo:
        result.warnings.append(
            "Workspace repo could not be detected: cross-repo restriction is disabled. "
            "Set WORKSPACE_REPO or pass --workspace-repo to enforce cross-repo policy."
        )
    elif repo != workspace_repo:
        result.reasons.append(
            f"Cross-repo PR ({repo}) is not eligible for workspace {workspace_repo}. "
            "Clear WORKSPACE_REPO to disable cross-repo restriction."
        )

    if pr.get("isDraft"):
        result.reasons.append("PR is still a draft")

    if pr.get("state") != "OPEN":
        result.reasons.append(f"PR state is {pr.get('state')}, not OPEN")

    status_checks = pr.get("statusCheckRollup", [])
    if not status_checks:
        result.warnings.append(
            "No CI checks configured; CI requirement satisfied without a green build"
        )
    elif not checks_green(status_checks):
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

    review_decision = pr.get("reviewDecision")
    if review_decision == "CHANGES_REQUESTED":
        result.reasons.append("Review decision: CHANGES_REQUESTED")
    elif review_decision not in (None, "", "REVIEW_REQUIRED", "APPROVED"):
        result.warnings.append(f"Review decision: {review_decision}")

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


def _resolve_workspace_repo(args: argparse.Namespace) -> str:
    """Resolve workspace repo from CLI args, env, or auto-detection."""
    cli_value = getattr(args, "workspace_repo", None)
    if isinstance(cli_value, str):
        return cli_value
    if "WORKSPACE_REPO" in os.environ:
        return os.environ["WORKSPACE_REPO"]  # honours "" to opt out
    return detect_workspace_repo()


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
        "--workspace-repo",
        help=(
            "Workspace repo (owner/name) for cross-repo policy. "
            "Auto-detected from git remote if not provided."
        ),
    )
    args = parser.parse_args()
    workspace_repo = _resolve_workspace_repo(args)

    try:
        repo, number = parse_pr_target(args.pr, args.repo, args.number)
        result = evaluate_pr(
            repo,
            number,
            workspace_repo=workspace_repo,
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
