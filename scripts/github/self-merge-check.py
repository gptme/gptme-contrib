#!/usr/bin/env python3
"""Check whether a PR qualifies for self-merge under the agent's self-merge policy.

Evaluates a PR against a conservative set of rules that determine whether an
autonomous AI agent can safely merge its own PRs without human review.

Policy summary:
- CI must be green (or skipped)
- A machine review must be present and clean: EITHER a Greptile review with no
  unresolved threads, OR a high-confidence review from the agent's own
  self-hosted AI reviewer (see "Accepting our own review" below)
- PR must be authored by the authenticated user
- Changed files must fall into a low-risk category (tests, docs, lessons, internal
  tooling, task metadata)
- Sensitive/security/infra paths immediately disqualify the PR

The workspace repo (the repo where the agent's brain lives) is detected automatically.
Cross-repo PRs (agent pushing to external repos) are disqualified by default.
To allow cross-repo merges, either:
- Set WORKSPACE_REPO to a comma- or whitespace-separated allowlist of owner/name
  entries (e.g. "ErikBjare/bob,gptme/gptme,gptme/gptme-contrib") — all other
  category/CI/Greptile checks still apply.
- Or clear WORKSPACE_REPO entirely (set it to empty string) to disable the
  cross-repo restriction (not recommended; widens the merge surface).

Accepting our own review
------------------------
Greptile does not run on every repo, and when it is absent this gate used to be
unsatisfiable — "Greptile review not found" blocked PRs that our own reviewer had
already reviewed in full (observed on gptme/gptme-contrib#1382). The reviewer's
verdict is now accepted as an *alternative* to Greptile. It is strictly an OR:
when Greptile has reviewed, nothing about this gate's behaviour changes, and the
marker is not even fetched.

The strictness here is set by which direction the errors run. The reviewer's
measured precision is 37%, but precision governs *false findings*, and a false
finding lowers the score, which makes this gate more conservative — it costs a
wasted round, not a bad merge. The dangerous direction is **recall**: a missed
bug produces a clean score and an unreviewed merge. So every condition below
exists to make "clean" mean "clean on real evidence", and anything short of that
falls back to requiring Greptile rather than approving:

- score is 5/5 — the rubric's only "nothing outstanding on this head" value.
  Deliberately not env-overridable, unlike the Greptile floor.
- the review is at the current head SHA (a review of an older push says nothing
  about the code about to merge)
- consensus is present and undegraded — no lost passes, no lost fan-out jobs, no
  clamped agreement threshold. A degraded run reached its score on less evidence
  than it asked for, which is exactly the recall failure. A **missing** consensus
  record means no consensus stage ran at all (the hourly sweep's single-pass
  reviews, or the agent engine); the reviewer documents that as "unknown, never
  read as full consensus", so it does not satisfy this gate either. Get a
  qualifying review with a consensus re-review: `ai-review.py OWNER/REPO N --force`.
- the reviewer did not abstain (a null score is "no verdict" — the submodule
  pointer-bump case — and must never read as a pass)
- the marker comment was posted by the authenticated user. Anyone can write the
  marker string into a comment on our PR; only our own account posts a real one.

Unresolved findings do not need a separate check: 5/5 is defined as zero findings
on the reviewed head, and the reviewer deletes its own superseded inline comments
on each re-review.

Usage:
    python3 scripts/github/self-merge-check.py <pr-url>
    python3 scripts/github/self-merge-check.py --repo gptme/gptme 123
    python3 scripts/github/self-merge-check.py --json <pr-url>

Environment:
    GH_RATE_LIMIT_HELPER
                    Optional explicit path to a GitHub API rate-limit gate
                    helper. When set, it must point to an executable helper or
                    the script exits with code 2 instead of silently bypassing
                    the gate.
                    Falls back to BOB_GH_RATE_LIMIT_HELPER for compatibility.
    FORCE_SELF_MERGE_CHECK
                    Set to bypass the optional GitHub API rate-limit gate.
                    Falls back to BOB_FORCE_SELF_MERGE_CHECK for compatibility.
    WORKSPACE_REPO  Allowlist of workspace repos (owner/name), comma- or
                    whitespace-separated. Auto-detected single repo used when
                    unset. Set to empty string to disable the cross-repo
                    restriction entirely.
    SELF_MERGE_ALLOWED_PATHS
                    Per-repo path-glob allowlist for files that don't match
                    the generic self-merge categories. Format:
                    "owner/repo:path-glob[,owner/repo:path-glob...]".
                    Uses repo-relative segment globs (* stays within one
                    directory segment; ** matches zero or more directories).
    SELF_MERGE_ACCEPT_AI_REVIEW
                    Set to 0/false/no/off to disable the self-hosted-AI-review
                    alternative entirely, restoring the Greptile-only gate.
                    Kill switch for the "Accepting our own review" path above.

Exit codes:
    0   Eligible for self-merge
    1   Not eligible for self-merge
    2   Error (invalid args, gh failure, or explicit gate-helper misconfig)
    76  Deferred by the GitHub API rate-limit gate
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
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import Any, Iterator, cast

MAX_GRAPHQL_PAGE_SIZE = 100
DEFAULT_MIN_GREPTILE_SCORE = 5

# The one score on the reviewer's 0-5 rubric that means "nothing outstanding on
# the commit reviewed". Not env-overridable, deliberately: SELF_MERGE_MIN_GREPTILE_SCORE
# can be lowered, and letting the same knob lower *this* floor would turn the
# alternative path into the weakest link in the gate.
AI_REVIEW_CLEAN_SCORE = 5
# Shortest marker SHA prefix accepted when matching against the PR head. The
# reviewer writes 12 hex chars; this only guards against a truncated/garbage
# marker whose 1-2 char "sha" would prefix-match almost any head.
MIN_SHA_PREFIX_LEN = 7

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
    # Downstream agent workspaces commonly use these loop-control entrypoints.
    # Guard both hyphenated and underscored spellings so naming drift still
    # forces human review.
    "scripts/session-bandit",
    "scripts/session_bandit",
    "scripts/state-delta",
    "scripts/state_delta",
    # Loop-orchestration entrypoints: the control path that selects work and
    # drives autonomous/monitoring runs (and can itself trigger self-merges).
    # A change here can alter how the loop gates its own merges, so it must go
    # through human review. Prefixes (no extension) catch hyphen/underscore and
    # nested layouts: scripts/autonomous-run.sh, scripts/autonomous-run-cc.sh,
    # scripts/runs/autonomous/autonomous-loop.sh, scripts/runs/github/project-monitoring.sh.
    "scripts/autonomous-run",
    "scripts/autonomous_run",
    "scripts/autonomous-loop",
    "scripts/autonomous_loop",
    "scripts/runs/autonomous/",
    "scripts/runs/github/project-monitoring",
    "scripts/runs/github/project_monitoring",
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
# Dependency lockfiles: always auto-generated; low risk, mandatory companion to new packages.
LOCKFILES = {
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "mix.lock",
}
# Exact full-path match — intentionally only root-level files.
# Nested Makefiles (e.g. scripts/Makefile, packages/Makefile) fall through to
# is_internal_tooling() instead. If that scope should change, extend this set.
BOT_CONFIG_FILES = {
    ".pre-commit-config.yaml",
    "Makefile",
}
BOT_CONFIG_PREFIXES = (".github/",)
SELF_MERGE_ALLOWED_PATHS_ENV = "SELF_MERGE_ALLOWED_PATHS"
SELF_MERGE_ACCEPT_AI_REVIEW_ENV = "SELF_MERGE_ACCEPT_AI_REVIEW"
GATE_HELPER_ENV = "GH_RATE_LIMIT_HELPER"
GATE_HELPER_ENV_LEGACY = "BOB_GH_RATE_LIMIT_HELPER"
FORCE_SELF_MERGE_CHECK_ENV = "FORCE_SELF_MERGE_CHECK"
FORCE_SELF_MERGE_CHECK_ENV_LEGACY = "BOB_FORCE_SELF_MERGE_CHECK"
GATE_DEFER_EXIT = 76


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
    # HEAD commit OID at time of check — callers should re-verify before merging
    head_sha: str = ""


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


def run_gh_checked(args: list[str], timeout: int = 30) -> str | None:
    """`run_gh`, but `None` means *the call failed* rather than *the result was empty*.

    `run_gh` collapses both into `""`, which is fine wherever an empty result and
    a failed lookup lead to the same decision. It is not fine where absence is
    treated as evidence: reading "no Greptile summary comment" out of a timed-out
    request turns an unknown into a clean bill of health, and here that unknown
    is what opens the AI-review fallback path.
    """
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
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None


def get_gh_user() -> str:
    return run_gh(["api", "user", "-q", ".login"]) or ""


@cache
def merge_permission(repo: str) -> bool | None:
    """Whether the authenticated user can merge PRs in ``repo``.

    Returns True if the user has push/maintain/admin on the repo, False if they
    explicitly lack it, or None if the permission could not be determined (gh
    error / unexpected payload). None is treated as "unknown" by the caller —
    a warning, not a disqualifier — so a transient API hiccup doesn't block a
    legitimate self-merge. A definitive False does disqualify: it means the
    merge attempt would fail with a permissions error (e.g. an agent that
    contributes to an upstream repo via a fork has pull-only access).
    """
    raw = run_gh(["api", f"repos/{repo}", "-q", ".permissions"])
    if not raw:
        return None
    try:
        perms = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(perms, dict):
        return None
    # If none of the expected keys are present the payload shape is unexpected —
    # treat as unknown so a future API schema change doesn't false-disqualify.
    if not any(k in perms for k in ("push", "maintain", "admin")):
        return None
    return bool(perms.get("push") or perms.get("maintain") or perms.get("admin"))


def _resolve_gate_helper(script_path: Path | None = None) -> str | None:
    """Resolve the optional GitHub API rate-limit gate helper."""

    explicit_helper = os.environ.get(GATE_HELPER_ENV) or os.environ.get(
        GATE_HELPER_ENV_LEGACY
    )
    if explicit_helper:
        helper = Path(explicit_helper).expanduser()
        # Determine which env var actually supplied the value for error messages
        source_env = (
            GATE_HELPER_ENV
            if GATE_HELPER_ENV in os.environ
            and os.environ[GATE_HELPER_ENV] == explicit_helper
            else GATE_HELPER_ENV_LEGACY
        )
        if not helper.is_file():
            raise RuntimeError(f"{source_env} points to a missing helper: {helper}")
        if not os.access(helper, os.X_OK):
            raise RuntimeError(f"{source_env} is not executable: {helper}")
        return str(helper)

    script = (script_path or Path(__file__)).resolve()
    if len(script.parents) < 3:
        return None

    # Auto-probe one explicit workspace-sibling location instead of walking
    # arbitrary ancestors and executing the first matching filename we find.
    repo_root = script.parents[2]
    candidate = repo_root.parent / "scripts" / "github-rate-limit-health.sh"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _maybe_defer_for_rate_limit(
    *, json_output: bool, script_path: Path | None = None
) -> int | None:
    if os.environ.get(FORCE_SELF_MERGE_CHECK_ENV) or os.environ.get(
        FORCE_SELF_MERGE_CHECK_ENV_LEGACY
    ):
        return None

    gate_helper = _resolve_gate_helper(script_path)
    if not gate_helper:
        return None

    try:
        gate = subprocess.run(
            [gate_helper, "--gate"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        # Gate itself failed — don't block the real check on a broken helper.
        return None

    if gate.returncode != GATE_DEFER_EXIT:
        return None

    msg = (
        "self-merge-check deferred: GitHub API rate limit over threshold "
        f"(gate exit {GATE_DEFER_EXIT}). Set {FORCE_SELF_MERGE_CHECK_ENV}=1 "
        f"(or {FORCE_SELF_MERGE_CHECK_ENV_LEGACY}=1) to override."
    )
    if json_output:
        print(json.dumps({"deferred": True, "reason": msg}))
    else:
        print(msg, file=sys.stderr)
    return GATE_DEFER_EXIT


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


def _parse_workspace_repos(value: str | None) -> list[str] | None:
    """Parse a WORKSPACE_REPO value into an allowlist.

    Returns:
        None       — caller passed None (detection failed / unset)
        []         — explicit empty-string opt-out of the cross-repo restriction
        list[str]  — one or more owner/repo entries, comma- or whitespace-separated

    Whitespace around entries is trimmed. Empty entries between separators
    (e.g. ``"a/b,, c/d"``) are silently dropped.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return []
    # Support both comma- and whitespace-separated input. Replace commas with
    # spaces then split on whitespace to normalize across ``"a/b,c/d"``,
    # ``"a/b c/d"``, and ``"a/b, c/d"``.
    return [r for r in stripped.replace(",", " ").split() if r]


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
        if pr.startswith(("http://", "https://")):
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
        # Two-positional `owner/repo <number>` form: pr is the repo, the PR
        # number was supplied as the separate positional argument.
        if pr.count("/") == 1 and number is not None:
            return pr, number
        raise ValueError(
            f"Unrecognized PR specifier: {pr!r}. "
            f"Use a PR URL, 'owner/repo#number', or 'owner/repo <number>'."
        )
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
    except OSError as exc:
        raise RuntimeError(f"Failed to run 'gh' for {repo}#{number}: {exc}") from exc
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
            "number,title,url,author,statusCheckRollup,isDraft,state,reviewDecision,headRefOid,mergeStateStatus",
        ]
    )
    if not raw:
        raise RuntimeError(f"Failed to fetch PR metadata for {repo}#{number}")

    pr = cast(dict[str, Any], json.loads(raw))

    # Defensive check: the returned PR number must match what was requested.
    # A cache key collision in the gh shim (e.g. graphql-attribution.sh with a
    # stride-2 hash) can return stale data for a different PR number, producing
    # wrong url/head_sha while the correct number comes from the function argument.
    # Incident: gptme-contrib#1257 returned #1256's url+SHA after stride-2 collision.
    returned_number = pr.get("number")
    if returned_number != number:
        raise RuntimeError(
            f"PR data mismatch for {repo}#{number}: gh returned data for #{returned_number!r}. "
            f"Possible cache key collision in gh wrapper or malformed payload. "
            f"Try GH_API_CACHE_TTL=0 to bypass the cache."
        )

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
                      body
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
    # page, and reviews is set from the first page's response (line 385).  The
    # `or []` guard would only fire if reviews were None, which is unreachable at
    # this point — use an explicit None-check to make the invariant clear.
    return (reviews if reviews is not None else []), all_threads


# Sentinel for "review_data was not provided by the caller".
# Distinguishes between:
#   - review_data=_UNSET  → helper should fetch on demand (standalone call)
#   - review_data=None    → caller already tried and the API failed; skip retry
# This prevents fetch_greptile_status and fetch_unresolved_human_threads from
# each making an independent GraphQL round-trip when evaluate_pr already tried
# and got None back (e.g. transient GitHub API failure).
_UNSET: object = object()


def fetch_greptile_status(
    repo: str,
    pr_number: int,
    review_data: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = _UNSET,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Check if Greptile reviewed this PR and count unresolved threads.

    Only considers threads from the most recent Greptile review cycle so that
    re-reviews after fixes are not blocked by old unresolved thread noise.

    Pass *review_data* (the return value of ``_fetch_greptile_review_data``)
    to avoid a duplicate GitHub API round-trip when the caller has already
    fetched this data.  Pass ``None`` explicitly to indicate the upstream fetch
    already failed — the helper will skip its own retry in that case.
    """
    if review_data is _UNSET:
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
        comments_raw = run_gh_checked(
            [
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--paginate",
                "--jq",
                '.[] | select(.user.login | test("greptile";"i")) | .id',
            ],
            timeout=30,
        )
        if comments_raw is None:
            # Could not check — unknown, never absence. Reporting "no Greptile
            # review" here would open the AI-review fallback on a transient
            # timeout and could merge a PR that Greptile had unresolved feedback
            # on, silently bypassing the review this gate exists to require.
            return {
                "has_review": False,
                "unresolved": 0,
                "total": 0,
                "unknown": True,
            }
        has_summary = bool(comments_raw.strip())
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


# Known bot logins (exact match, case-insensitive) to exclude when counting
# human review threads.  GitHub Apps append "[bot]" to their login, so any
# login ending with that suffix is also excluded.
_BOT_EXACT_NAMES = frozenset(("greptile", "codecov", "greptile-apps", "greptileai"))


def _is_bot_author(author: str) -> bool:
    """Return True if the GitHub login belongs to a bot / GitHub App."""
    lower = author.lower()
    return lower.endswith("[bot]") or lower in _BOT_EXACT_NAMES


# Inline findings posted by Bob's self-hosted AI reviewer carry this marker.
# They are machine-authored but posted through Bob's *user* account rather than
# a GitHub App, so `_is_bot_author` cannot see them — the login is a perfectly
# ordinary human-looking one.
_AI_REVIEW_FINDING_MARKER = "<!-- bob-ai-review-finding -->"


def _is_ai_review_thread(comments: list[dict[str, Any]]) -> bool:
    """True when a review thread was opened by an automated reviewer.

    Without this, Bob's own AI review makes its PRs *less* mergeable: each
    inline finding opens a thread attributed to Bob's user account, which
    ``fetch_unresolved_human_threads`` then reports as unacknowledged human
    review feedback and the self-merge gate blocks on. Observed live on
    gptme/gptme-contrib#1383 — "4 unresolved human review thread(s) from:
    TimeToBuildBob", where all four were the AI reviewer's own findings.

    Detection is by explicit marker rather than by author login, because the
    login is shared with Bob's genuine human-directed comments; only the
    reviewer stamps this marker.
    """
    if not comments:
        return False
    return _AI_REVIEW_FINDING_MARKER in (comments[0].get("body") or "")


# Summary marker upserted by the self-hosted reviewer onto one issue comment per
# PR: `<!-- bob-ai-review {json} -->`. The space before `{` is load-bearing — it
# is what stops this pattern from also matching `<!-- bob-ai-review-finding -->`
# and the per-finding `<!-- bob-ai-review-fp {...} -->`.
_AI_REVIEW_MARKER_PREFIX = "<!-- bob-ai-review "
_JSON_DECODER = json.JSONDecoder()


def _iter_ai_review_markers(body: str) -> Iterator[dict[str, Any]]:
    """Yield complete JSON objects from self-hosted-review summary markers.

    Regex cannot safely delimit JSON because real markers contain nested objects.
    ``raw_decode`` consumes exactly one complete JSON value, after which we verify
    the marker suffix so arbitrary prose containing the prefix is ignored.
    """
    offset = 0
    while (start := body.find(_AI_REVIEW_MARKER_PREFIX, offset)) != -1:
        json_start = start + len(_AI_REVIEW_MARKER_PREFIX)
        try:
            parsed, json_end = _JSON_DECODER.raw_decode(body, json_start)
        except json.JSONDecodeError:
            offset = json_start
            continue
        if body.startswith(" -->", json_end) and isinstance(parsed, dict):
            yield parsed
        offset = max(json_end, json_start + 1)


def _ai_review_enabled() -> bool:
    """Whether our own review may satisfy the machine-review requirement."""
    raw = os.environ.get(SELF_MERGE_ACCEPT_AI_REVIEW_ENV, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _sha_matches_head(marker_sha: str, head_sha: str) -> bool:
    """Whether a marker's abbreviated SHA names the PR's current head."""
    marker_sha = (marker_sha or "").strip().lower()
    head_sha = (head_sha or "").strip().lower()
    if len(marker_sha) < MIN_SHA_PREFIX_LEN or len(head_sha) < MIN_SHA_PREFIX_LEN:
        return False
    # The marker may abbreviate the authoritative head, never the reverse.
    return head_sha.startswith(marker_sha)


def _consensus_shortfall(consensus: Any) -> str | None:
    """Why a review's consensus is not trustworthy evidence, or None if it is.

    Mirrors the reviewer's own ``consensus_degraded`` (lost passes OR lost
    fan-out jobs) and additionally rejects a *clamped* agreement threshold and a
    run where nothing at all answered:

    - lost passes / lost jobs: the score was reached on less evidence than was
      requested. Job loss is the one that matters in practice -- a pass counts as
      "answered" when any single one of its aspect jobs returns, so a wave can
      lose two thirds of its jobs and still report every pass answered.
    - clamped agreement: the filter that ran is weaker than the filter asked for.
    - zero answered: every call failed, so there are no findings, so the score
      computes to a clean 5/5 off an empty result. That is the exact recall
      failure this whole function exists to catch.
    """
    if not isinstance(consensus, dict):
        # Absent or malformed. The reviewer's contract is explicit that a missing
        # record means "no consensus stage ran" (single-pass sweep review, or the
        # agent engine) and must be read as unknown -- never as full consensus.
        return "no consensus record (single-pass or agent review)"

    def _int(key: str) -> int | None:
        value = consensus.get(key)
        # bool is an int subclass in Python, but it is never a valid count.
        return value if type(value) is int else None

    requested, answered = _int("requested"), _int("answered")
    jobs_requested, jobs_answered = _int("jobs_requested"), _int("jobs_answered")
    if requested is None or answered is None:
        return "consensus record is missing pass counts"
    if jobs_requested is None or jobs_answered is None:
        return "consensus record is missing job counts"

    # One pass is the sweep's non-consensus mode even if a marker explicitly
    # serializes 1/1 counts. Qualifying evidence must contain multiple independent
    # passes, not merely a present consensus object.
    if requested < 2:
        return "consensus requires at least 2 requested passes"
    if answered < 1 or jobs_answered < 1:
        return "no consensus pass answered"
    if answered < requested:
        return f"consensus degraded ({answered}/{requested} passes answered)"
    if answered > requested:
        return f"consensus has invalid pass counts ({answered}/{requested} answered)"
    if jobs_answered < jobs_requested:
        return f"consensus degraded ({jobs_answered}/{jobs_requested} jobs answered)"
    if jobs_answered > jobs_requested:
        return (
            f"consensus has invalid job counts "
            f"({jobs_answered}/{jobs_requested} answered)"
        )

    applied, asked = _int("min_agreement"), _int("min_agreement_requested")
    if applied is None or asked is None:
        return "consensus record is missing agreement thresholds"
    # A threshold below 1 is not a weaker consensus, it is *no* consensus: a
    # finding surviving on zero agreeing passes is exactly what a single pass
    # already produces. Checking only `applied < asked` misses it, because a
    # record with min_agreement 0 AND min_agreement_requested 0 is unclamped and
    # would read as full consensus — while `answered >= 1` above is satisfied by
    # one pass. That combination hands the gate a single-pass review dressed as
    # consensus, gutting the recall guard this whole path exists to enforce.
    # Negatives are rejected by the same test rather than a separate one; both
    # mean "no agreement was required".
    if applied < 1 or asked < 1:
        return (
            f"consensus agreement threshold below 1 "
            f"(applied {applied}, requested {asked})"
        )
    if applied < asked:
        return f"consensus agreement clamped to {applied} (requested {asked})"
    return None


def _fetch_ai_review_marker(
    repo: str, pr_number: int, *, expected_author: str
) -> dict[str, Any] | None:
    """Latest self-hosted-review summary marker on the PR, parsed.

    Only comments authored by *expected_author* are considered: the marker is
    plain text in a comment body, so any account that can comment on the PR could
    otherwise forge a clean verdict for a gate that then merges without a human.
    An empty *expected_author* means the identity could not be established, and
    the marker is not trusted at all.
    """
    if not expected_author:
        return None
    raw = run_gh(
        [
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--paginate",
            "--jq",
            ".[] | {author: .user.login, body: .body}",
        ],
        timeout=30,
    )
    if not raw:
        return None

    latest: dict[str, Any] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comment = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(comment, dict) or comment.get("author") != expected_author:
            continue
        # Last match wins within a body, and the last qualifying comment wins
        # overall: the reviewer upserts a single comment, but a repo with legacy
        # append-style comments should still be read at its most recent verdict.
        for parsed in _iter_ai_review_markers(comment.get("body") or ""):
            latest = parsed
    return latest


def fetch_ai_review_status(
    repo: str, pr_number: int, *, head_sha: str, expected_author: str
) -> dict[str, Any]:
    """Whether our own reviewer's verdict is strong enough to stand in for Greptile.

    Returns ``{"accepted": bool, "detail": str | None}``. ``detail`` explains a
    refusal so the gate's "Greptile review not found" reason can say *why* the
    fallback did not apply, rather than leaving the operator to guess whether the
    reviewer ran at all.

    Every refusal path falls back to today's Greptile-only behaviour. Nothing
    here can make a PR *more* mergeable than the rest of the gate allows -- CI,
    category, human threads and author identity are all still evaluated.
    """
    if not _ai_review_enabled():
        return {"accepted": False, "detail": None}

    marker = _fetch_ai_review_marker(repo, pr_number, expected_author=expected_author)
    if marker is None:
        return {"accepted": False, "detail": None}

    if not _sha_matches_head(str(marker.get("sha") or ""), head_sha):
        return {
            "accepted": False,
            "detail": (
                f"AI review is stale (reviewed {marker.get('sha') or 'unknown'}, "
                f"head is {head_sha[:12]})"
            ),
        }

    score = marker.get("score")
    if score is None:
        return {"accepted": False, "detail": "AI reviewer abstained (no score)"}
    # Type and threshold are separate refusals so the detail names the actual
    # defect. Folding them together reported a malformed marker (score "5" as a
    # string, or 5.0) as `AI review score 5/5 below 5/5` — a sentence that is
    # false on its face and sends whoever reads it hunting for a scoring bug
    # instead of the marker corruption that caused it. Both still fail closed.
    # `type(...) is not int` rather than `isinstance`, matching `_int` above:
    # bool is an int subclass, and `True` is not a review score.
    if type(score) is not int:
        return {
            "accepted": False,
            "detail": (
                f"AI review score is not an integer "
                f"({type(score).__name__}: {score!r})"
            ),
        }
    # Exactly the rubric's clean value, not "at least" it. `confidence_score`
    # emits 1-5 and nothing else, so a 6 is not a better review — it is a
    # corrupted or foreign marker, and `<` would wave it through as clean while
    # every other check (author, SHA, consensus) still passed.
    if score != AI_REVIEW_CLEAN_SCORE:
        below = "below" if score < AI_REVIEW_CLEAN_SCORE else "outside the rubric,"
        return {
            "accepted": False,
            "detail": f"AI review score {score}/5 {below} {AI_REVIEW_CLEAN_SCORE}/5",
        }

    shortfall = _consensus_shortfall(marker.get("consensus"))
    if shortfall:
        return {"accepted": False, "detail": f"AI review {shortfall}"}

    return {
        "accepted": True,
        "detail": (
            f"AI review {score}/5 at current head {str(marker.get('sha'))[:12]}, "
            "full consensus"
        ),
    }


def fetch_unresolved_human_threads(
    repo: str,
    pr_number: int,
    review_data: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = _UNSET,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Count unresolved review threads from human (non-bot) reviewers.

    Uses the same GraphQL data as ``fetch_greptile_status`` but filters for
    threads authored by humans.  Unresolved human threads indicate review
    feedback that has not been acknowledged — merging over them is the exact
    pattern Erik flagged on gptme-contrib#537.

    Pass *review_data* (the return value of ``_fetch_greptile_review_data``)
    to avoid a duplicate GitHub API round-trip when the caller has already
    fetched this data.  Pass ``None`` explicitly to indicate the upstream fetch
    already failed — the helper will skip its own retry in that case.
    """
    if review_data is _UNSET:
        review_data = _fetch_greptile_review_data(repo, pr_number)
    if review_data is None:
        return {"unresolved": 0, "total": 0, "authors": []}

    _, threads = review_data

    total = 0
    unresolved = 0
    unresolved_authors: set[str] = set()

    for thread in threads:
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = (comments[0].get("author") or {}).get("login", "")
        if _is_bot_author(author):
            continue
        # Automated review findings are not human feedback, even though they
        # are posted through a user account rather than a GitHub App.
        if _is_ai_review_thread(comments):
            continue
        total += 1
        if not thread.get("isResolved", False):
            unresolved += 1
            unresolved_authors.add(author)

    return {
        "unresolved": unresolved,
        "total": total,
        "authors": sorted(unresolved_authors),
    }


def checks_green(status_checks: list[dict[str, Any]]) -> bool:
    """Return True if all reported checks are success/skipped/neutral.

    Returns True when no checks are configured (no CI), treating the
    absence of CI as neutral rather than failing. Individual checks with no
    status and no conclusion are treated as indeterminate, not passing.

    Handles both check shapes in ``statusCheckRollup``:
    - ``CheckRun`` (GitHub Actions) — result in ``status``/``conclusion``
    - ``StatusContext`` (legacy commit statuses, e.g. ``codecov/patch``) —
      result in ``state`` alone, with no ``status``/``conclusion``. Reading
      only status/conclusion would treat a green StatusContext as
      indeterminate and falsely flag CI as not green.
    """
    if not status_checks:
        return True
    allowed = {"SUCCESS", "SKIPPED", "NEUTRAL"}
    for check in status_checks:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        state = (check.get("state") or "").upper()
        # StatusContext entries carry their result in `state` alone.
        if state and not status and not conclusion:
            if state not in allowed:
                return False
            continue
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
    """Spec-like identity docs (README/ARCHITECTURE/CLAUDE/…) require human review.

    Only matches at the REPOSITORY ROOT. These filenames denote a project's
    top-level identity/spec docs; a *nested* package README (e.g.
    ``packages/foo/README.md``) is ordinary package documentation, not a spec,
    and shouldn't block self-merge of an otherwise low-risk tooling PR.
    """
    normalized = path.replace("\\", "/").removeprefix("./")
    return "/" not in normalized and Path(path).name in SPEC_LIKE_DOCS


def is_test_file(path: str) -> bool:
    return any(marker in path for marker in TEST_MARKERS)


def is_internal_tooling(path: str) -> bool:
    return path.startswith(INTERNAL_TOOLING_PREFIXES)


def is_task_metadata(path: str) -> bool:
    return path.startswith(TASK_METADATA_PREFIXES)


def is_lesson_file(path: str) -> bool:
    return path.startswith(LESSON_PREFIXES)


def is_lockfile(path: str) -> bool:
    """Return True for dependency lockfiles (auto-generated, low-risk).

    These are mandatory companions to new package additions in uv/poetry/npm
    workspaces and must not block self-merge purely because the tool regenerated
    them.  Only the exact filename is checked; the file must sit at the root of
    the repository or inside a workspace member directory.
    """
    return path in LOCKFILES or path.rsplit("/", 1)[-1] in LOCKFILES


def is_bot_config(path: str) -> bool:
    return path in BOT_CONFIG_FILES or path.startswith(BOT_CONFIG_PREFIXES)


def _parse_repo_path_allowlist(value: str | None) -> dict[str, list[str]]:
    """Parse SELF_MERGE_ALLOWED_PATHS into a repo -> glob patterns mapping.

    Format:
        owner/repo:path-glob[,owner/repo:path-glob...]

    Both commas and whitespace may separate entries. Invalid entries are ignored.
    """
    if value is None:
        return {}
    stripped = value.strip()
    if not stripped:
        return {}

    mapping: dict[str, list[str]] = {}
    for entry in stripped.replace(",", " ").split():
        repo, sep, pattern = entry.partition(":")
        if not sep or not repo or not pattern:
            continue
        mapping.setdefault(repo, []).append(pattern.replace("\\", "/"))
    return mapping


def _get_repo_path_allowlist() -> dict[str, list[str]]:
    return _parse_repo_path_allowlist(os.environ.get(SELF_MERGE_ALLOWED_PATHS_ENV))


def _path_glob_match(path: str, pattern: str) -> bool:
    """Match repo-relative paths with shell-style globs.

    `*` stays within one path segment. A standalone `**` segment matches zero or
    more full directory segments, which keeps the allowlist semantics stable
    across Python versions and matches normal globstar expectations.
    """
    path_parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(
        part for part in pattern.replace("\\", "/").split("/") if part
    )

    @cache
    def _match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)

        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return _match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and _match(path_index + 1, pattern_index)
            )

        if path_index >= len(path_parts):
            return False
        if not fnmatchcase(path_parts[path_index], pattern_part):
            return False
        return _match(path_index + 1, pattern_index + 1)

    return _match(0, 0)


def is_repo_allowlisted_path(
    path: str,
    repo: str | None,
    repo_path_allowlist: dict[str, list[str]] | None = None,
) -> bool:
    if not repo:
        return False
    allowlist = (
        repo_path_allowlist
        if repo_path_allowlist is not None
        else _get_repo_path_allowlist()
    )
    if not allowlist:
        return False
    normalized = path.replace("\\", "/")
    return any(
        _path_glob_match(normalized, pattern) for pattern in allowlist.get(repo, [])
    )


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


def is_allowed_file(
    path: str,
    repo: str | None = None,
    repo_path_allowlist: dict[str, list[str]] | None = None,
) -> bool:
    """Check if a file falls into any allowed self-merge category."""
    # Hard-reject bot config and spec-like docs regardless of allowlist — these
    # categories require human review regardless of what the operator has allowlisted.
    if is_bot_config(path):
        return False
    if is_doc_file(path) and is_spec_like_doc(path):
        return False
    # Explicit repo allowlist overrides only the sensitive-path keyword heuristic.
    # If an operator has deliberately allowlisted a path, that decision stands even
    # when the keyword scanner would flag it (e.g. LLM tokenizer files named tokens.py),
    # but bot-config and spec-like-doc guards above are not bypassable.
    if is_repo_allowlisted_path(path, repo, repo_path_allowlist):
        return True
    if is_sensitive_path(path):
        return False
    return (
        is_test_file(path)
        or is_lesson_file(path)
        or is_task_metadata(path)
        or is_internal_tooling(path)
        or is_lockfile(path)
        or is_doc_file(path)
    )


def classify_category(
    paths: list[str], repo: str | None = None
) -> tuple[str | None, list[str]]:
    """Return the allowed category, or None with disqualifying reasons.

    The policy says all changed files must fall into *one of* the allowed
    categories. A PR mixing scripts/ and tasks/ files is still low-risk and
    eligible as "mixed-allowed".
    """
    reasons: list[str] = []
    if not paths:
        return None, ["PR has no changed files"]

    # Build the allowlist before the sensitive-path check so that explicitly
    # allowlisted paths can override the keyword heuristic (e.g. an LLM tokenizer
    # file named tokens.py passes when the operator adds it to SELF_MERGE_ALLOWED_PATHS).
    repo_path_allowlist = _get_repo_path_allowlist()

    if any(
        is_sensitive_path(path)
        and not is_repo_allowlisted_path(path, repo, repo_path_allowlist)
        for path in paths
    ):
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
    if all(is_lockfile(path) for path in paths):
        return "lockfiles-only", reasons
    if all(is_doc_file(path) for path in paths):
        return "docs-only", reasons
    if repo and all(
        is_repo_allowlisted_path(path, repo, repo_path_allowlist) for path in paths
    ):
        return f"repo-allowlisted({repo})", reasons

    # Mixed-category: eligible if ALL files are in some allowed category
    if all(is_allowed_file(path, repo, repo_path_allowlist) for path in paths):
        categories: list[str] = []
        if any(is_test_file(path) for path in paths):
            categories.append("tests")
        if any(is_lesson_file(path) for path in paths):
            categories.append("lessons")
        if any(is_task_metadata(path) for path in paths):
            categories.append("task-metadata")
        if any(is_internal_tooling(path) for path in paths):
            categories.append("internal-tooling")
        if any(is_lockfile(path) for path in paths):
            categories.append("lockfiles")
        if any(is_doc_file(path) for path in paths):
            categories.append("docs")
        if repo and any(
            is_repo_allowlisted_path(path, repo, repo_path_allowlist) for path in paths
        ):
            categories.append("repo-paths")
        return f"mixed-allowed({'+'.join(categories)})", reasons

    disqualifying = [
        p for p in paths if not is_allowed_file(p, repo, repo_path_allowlist)
    ]
    return None, [
        f"Files not in any allowed self-merge category: {', '.join(disqualifying)}"
    ]


def _check_workspace_repo(
    repo: str, workspace_repos: list[str] | None
) -> tuple[list[str], list[str]]:
    """Evaluate the cross-repo policy against a PR's repo.

    Returns a ``(reasons, warnings)`` pair. ``reasons`` disqualify the PR from
    self-merge; ``warnings`` are informational only.

    Semantics:
    - ``workspace_repos is None``  → detection failed, disqualify (we cannot
      tell whether the PR is cross-repo)
    - ``workspace_repos == []``    → explicit opt-out, emit a warning but do
      not disqualify on cross-repo grounds
    - ``repo in workspace_repos``  → allowlisted, no reasons or warnings
    - otherwise                    → cross-repo, disqualify
    """
    if workspace_repos is None:
        return (
            [
                "Workspace repo could not be auto-detected; cross-repo restriction cannot be "
                "enforced. Pass --workspace-repo or set WORKSPACE_REPO (use '' to opt out)."
            ],
            [],
        )
    if not workspace_repos:
        return (
            [],
            [
                "Workspace repo check disabled via WORKSPACE_REPO='' or --workspace-repo ''; "
                "cross-repo restriction is not enforced."
            ],
        )
    if repo not in workspace_repos:
        allowed = ", ".join(workspace_repos)
        return (
            [
                f"Cross-repo PR ({repo}) is not in allowed workspace repos: {allowed}. "
                "Clear WORKSPACE_REPO or add this repo to the allowlist."
            ],
            [],
        )
    return [], []


def greptile_summary_score(repo: str, pr_number: int) -> int | None:
    """Latest Greptile summary score via the shared greptile-merge-signal evaluator.

    Returns the parsed score (0-5), or None if Greptile posted no parseable summary score.

    Note on env-var precedence: this function only extracts the raw score from the
    subprocess JSON. The floor comparison uses SELF_MERGE_MIN_GREPTILE_SCORE (parsed
    by _parse_self_merge_min_greptile_score). The subprocess's own threshold
    GREPTILE_MERGE_SIGNAL_MIN_SCORE has no effect on the gate — setting it does not
    soften or tighten the floor here.
    """
    signal = Path(__file__).with_name("greptile-merge-signal.py")
    if not signal.exists():
        return None
    try:
        env = os.environ.copy()
        # The self-merge gate owns its own enable/disable policy; don't let the
        # standalone signal tool's disable flag silently neuter the floor.
        env.pop("GREPTILE_MERGE_SIGNAL_DISABLED", None)
        proc = subprocess.run(
            [sys.executable, str(signal), "--repo", repo, str(pr_number)],
            capture_output=True,
            env=env,
            text=True,
            timeout=30,
        )
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    score = data.get("score")
    return int(score) if isinstance(score, int) else None


def _parse_self_merge_min_greptile_score() -> int:
    raw = os.environ.get("SELF_MERGE_MIN_GREPTILE_SCORE", "").strip()
    if not raw:
        return DEFAULT_MIN_GREPTILE_SCORE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MIN_GREPTILE_SCORE
    if 0 <= value <= 5:
        return value
    return DEFAULT_MIN_GREPTILE_SCORE


def evaluate_pr(
    repo: str, number: int, *, workspace_repos: list[str] | None
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
        head_sha=pr.get("headRefOid", ""),
    )

    current_user = get_gh_user()
    if not current_user:
        result.reasons.append(
            "Could not determine authenticated GitHub user; author-identity check failed."
        )
    elif author != current_user:
        result.reasons.append(f"PR author is {author}, expected {current_user}")

    cross_repo_reasons, cross_repo_warnings = _check_workspace_repo(
        repo, workspace_repos
    )
    result.reasons.extend(cross_repo_reasons)
    result.warnings.extend(cross_repo_warnings)

    # Merge-permission check: an allowlisted repo the agent cannot actually merge
    # (pull-only access, e.g. an upstream repo contributed to via a fork) would
    # otherwise report eligible=YES and then fail the merge with a GitHub
    # permissions error. Disqualify on a definitive lack of permission; treat an
    # indeterminate result (gh error) as a non-blocking warning.
    can_merge = merge_permission(repo)
    if can_merge is False:
        result.reasons.append(
            f"Authenticated user lacks merge permission on {repo} "
            "(pull-only access; merge would fail). Open a PR for a maintainer to merge."
        )
    elif can_merge is None:
        result.warnings.append(
            f"Could not determine merge permission on {repo}; "
            "merge may fail if access is pull-only."
        )

    if pr.get("isDraft"):
        result.reasons.append("PR is still a draft")

    if pr.get("state") != "OPEN":
        result.reasons.append(f"PR state is {pr.get('state')}, not OPEN")

    merge_state = pr.get("mergeStateStatus", "")
    if merge_state == "DIRTY":
        result.reasons.append(
            f"PR has merge conflicts (mergeStateStatus: {merge_state})"
        )

    status_checks = pr.get("statusCheckRollup", [])
    if not status_checks:
        result.warnings.append(
            "No CI checks configured; CI requirement satisfied without a green build"
        )
    elif not checks_green(status_checks):
        result.reasons.append("CI is not fully green")

    # Fetch once; pass to both helpers to avoid duplicate GraphQL round-trips.
    shared_review_data = _fetch_greptile_review_data(repo, number)

    greptile = fetch_greptile_status(repo, number, review_data=shared_review_data)
    if not greptile["has_review"]:
        # The alternative review is safe only after a successful GraphQL fetch
        # proves there is no Greptile review. An API failure is unknown, not
        # absence: accepting our marker there would turn a fail-closed outage into
        # a path around potentially unresolved Greptile feedback.
        # `greptile["unknown"]` covers the second way the lookup can fail: the
        # GraphQL fetch succeeds and reports no reviews, but the summary-comment
        # fallback inside `fetch_greptile_status` errors out. That path also
        # yields `has_review: False`, so gating on `shared_review_data` alone
        # would let a transient comment-API failure open the AI fallback.
        if shared_review_data is None or greptile.get("unknown"):
            result.reasons.append(
                "Could not verify Greptile review state; refusing AI-review fallback"
            )
        else:
            ai_review = fetch_ai_review_status(
                repo,
                number,
                head_sha=result.head_sha,
                expected_author=current_user,
            )
            if ai_review["accepted"]:
                result.warnings.append(
                    f"Greptile review not found; satisfied by self-hosted "
                    f"AI review ({ai_review['detail']})"
                )
            else:
                reason = "Greptile review not found"
                if ai_review["detail"]:
                    reason += f"; {ai_review['detail']}"
                result.reasons.append(reason)
    elif greptile["unresolved"] > 0:
        result.reasons.append(
            f"Greptile has {greptile['unresolved']} unresolved review thread(s)"
        )

    # Score floor (defense in depth): require the Greptile summary score >= floor
    # (default 5/5, override via SELF_MERGE_MIN_GREPTILE_SCORE). Without this, resolving
    # threads alone could let a low-score PR self-merge — the alice#61 (4/5) and #63 (3/5)
    # incidents where buggy control-path code shipped. Reuses the shared greptile-merge-signal
    # evaluator (upstreamed from Bob). Parse failure (score None) does NOT block — the
    # thread/category gates still apply; this only catches a clear sub-floor score.
    if greptile["has_review"]:
        min_score = _parse_self_merge_min_greptile_score()
        score = greptile_summary_score(repo, number)
        if score is not None and score < min_score:
            result.reasons.append(f"Greptile score {score}/5 below floor {min_score}/5")

    human_threads = fetch_unresolved_human_threads(
        repo, number, review_data=shared_review_data
    )
    if human_threads["unresolved"] > 0:
        authors = ", ".join(human_threads["authors"]) or "unknown"
        result.reasons.append(
            f"{human_threads['unresolved']} unresolved human review thread(s) "
            f"from: {authors}"
        )

    category, category_reasons = classify_category(files, repo=repo)
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


def _resolve_workspace_repos(args: argparse.Namespace) -> list[str] | None:
    """Resolve the workspace-repo allowlist from CLI args, env, or auto-detection.

    ``--workspace-repo`` / ``WORKSPACE_REPO`` accept a comma- or whitespace-
    separated list of ``owner/repo`` entries.

    Returns:
        list[str]  — one or more allowed workspace repos
        []         — explicit opt-out via empty string (cross-repo restriction disabled)
        None       — detection was attempted but failed; cross-repo restriction
                     will disqualify because we cannot tell whether the PR is cross-repo
    """
    cli_value: str | None = getattr(args, "workspace_repo", None)
    if cli_value is not None:
        return _parse_workspace_repos(cli_value)
    if "WORKSPACE_REPO" in os.environ:
        return _parse_workspace_repos(os.environ["WORKSPACE_REPO"])
    detected = detect_workspace_repo()
    return [detected] if detected else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pr", nargs="?", help="PR URL, repo#num, or PR number")
    parser.add_argument(
        "number",
        nargs="?",
        type=int,
        help="PR number when using --repo or 'owner/repo <number>' form",
    )
    parser.add_argument("--repo", help="Repository in owner/name form")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--workspace-repo",
        help=(
            "Workspace-repo allowlist (owner/name, comma- or whitespace-separated) "
            "for cross-repo policy. Auto-detected single repo from git remote if "
            "not provided. Pass '' to disable the cross-repo restriction entirely."
        ),
    )
    args = parser.parse_args()
    workspace_repos = _resolve_workspace_repos(args)

    try:
        deferred = _maybe_defer_for_rate_limit(json_output=args.json)
        if deferred is not None:
            return deferred
        repo, number = parse_pr_target(args.pr, args.repo, args.number)
        result = evaluate_pr(
            repo,
            number,
            workspace_repos=workspace_repos,
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
        if workspace_repos:
            print(f"Workspace repos: {', '.join(workspace_repos)}")
        print(format_human(result))

    return 0 if result.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
