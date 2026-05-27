"""GitHub issue-resolver orchestrator.

Opt-in companion to `scripts/github_hygiene/issue_hygiene.py`. Triggered by a
`gptme-resolve` label or a `/gptme-resolve` comment from a trusted author,
this script:

1. Reads the issue body + metadata.
2. Runs `gptme` with a narrow resolver prompt in the checked-out repo.
3. Parses gptme's trailing `RESOLVER_STATUS:` marker.
4. If gptme reported changes AND git detects modified/untracked files, pushes
   an attempt branch and opens a DRAFT PR linked to the issue.
5. If gptme reported `no_changes` OR there is nothing to commit, pushes any
   partial attempt branch (if there are uncommitted changes) and posts a
   failure explanation comment on the issue.

Phase-1 safety invariants:

- Always DRAFT PRs. Never auto-merge.
- Never close the source issue.
- Branch name is derived from the issue number and is stable across
  re-triggers (`gptme-resolver/issue-<N>`).
- Idempotent: re-triggering overwrites the attempt branch with the new state.

The orchestrator is intentionally separate from `gptme` itself so the action
stays debuggable offline (unit tests can stub `run_gptme` and `git_*`).

Usage (inside the workflow):

    python scripts/github_resolver/resolve_issue.py \\
        --repo "$GITHUB_REPOSITORY" \\
        --issue "$ISSUE_NUMBER" \\
        --output-dir output

Environment:
    GH_TOKEN     — token with `contents: write` + `pull-requests: write` scopes
    GPTME_MODEL  — optional model override
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

# Make the sibling `github_actions_common` package importable when the script
# is run directly inside the Action workflow (no editable install).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_actions_common import (  # noqa: E402
    Issue,
    fetch_issue,
    gh,
    post_issue_comment,
    write_output,
)

MARKER_COMMENT = "<!-- gptme-issue-resolver: v1 -->"
STATUS_RE = re.compile(r"^RESOLVER_STATUS:\s*(changes|no_changes)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^RESOLVER_SUMMARY:\s*(.+?)\s*$", re.MULTILINE)
REASON_RE = re.compile(r"^RESOLVER_REASON:\s*(.+?)\s*$", re.MULTILINE)
BRANCH_PREFIX = "gptme-resolver/issue-"
RESOLVER_TOOL_ALLOWLIST = "read,save,patch,shell"
READONLY_GIT_SUBCOMMANDS = (
    "blame",
    "cat-file",
    "describe",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
    "symbolic-ref",
)
BLOCKED_AGENT_ENV_VARS = (
    "GH_ENTERPRISE_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
    "SSH_AUTH_SOCK",
)


@dataclass
class ResolverOutcome:
    status: str  # "changes" | "no_changes" | "error"
    summary: str
    branch: str | None
    pr_url: str | None


@dataclass(frozen=True)
class RepoState:
    head: str
    branch: str | None


def git(args: list[str], *, check: bool = True, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=check, cwd=cwd
    )
    return result.stdout


def render_prompt(template: str, *, repo: str, issue: Issue) -> str:
    labels_block = ", ".join(issue.labels) or "(none)"
    return template.format(
        repo=repo,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_author=issue.author,
        issue_labels=labels_block,
        issue_body=issue.body or "(empty body)",
    )


def run_gptme(
    prompt: str, *, model: str | None = None, workdir: Path
) -> tuple[str, str]:
    """Invoke gptme non-interactively. Fail soft: return (stdout, stderr)."""
    cmd = [
        "gptme",
        "--non-interactive",
        "--no-confirm",
        "--tools",
        RESOLVER_TOOL_ALLOWLIST,
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")
    try:
        with tempfile.TemporaryDirectory(prefix="gptme-resolver-shims-") as shim_dir:
            shim_path = Path(shim_dir)
            write_resolver_command_shims(shim_path)
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                cwd=workdir,
                env=build_agent_env(os.environ, shim_path),
            )
    except FileNotFoundError:
        msg = "gptme not found on PATH; skipping resolver.\n"
        sys.stderr.write(msg)
        return "", msg
    if result.returncode != 0:
        sys.stderr.write(f"gptme failed (rc={result.returncode}):\n{result.stderr}\n")
    return result.stdout, result.stderr


def parse_status(gptme_output: str) -> tuple[str, str]:
    """Return (status, message).

    status in {"changes", "no_changes", "error"}
    message is the SUMMARY (on changes) or REASON (on no_changes). On missing
    marker, status is "error" and message is a short explanation.
    """
    match = STATUS_RE.search(gptme_output)
    if not match:
        return "error", "gptme produced no RESOLVER_STATUS marker"
    status = match.group(1)
    if status == "changes":
        m = SUMMARY_RE.search(gptme_output)
        return "changes", (m.group(1).strip() if m else "(no summary provided)")
    m = REASON_RE.search(gptme_output)
    return "no_changes", (m.group(1).strip() if m else "(no reason provided)")


def has_git_changes(cwd: Path) -> bool:
    out = git(["status", "--porcelain"], cwd=cwd)
    return bool(out.strip())


def current_branch(cwd: Path) -> str | None:
    out = git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False, cwd=cwd)
    branch = out.strip()
    return branch or None


def capture_repo_state(cwd: Path) -> RepoState:
    return RepoState(
        head=git(["rev-parse", "HEAD"], cwd=cwd).strip(),
        branch=current_branch(cwd),
    )


def detect_repo_state_violation(before: RepoState, cwd: Path) -> str | None:
    after = capture_repo_state(cwd)
    violations: list[str] = []
    if after.branch != before.branch:
        before_branch = before.branch or "(detached HEAD)"
        after_branch = after.branch or "(detached HEAD)"
        violations.append(f"branch changed from {before_branch} to {after_branch}")
    if after.head != before.head:
        branch_desc = after.branch or before.branch or "(detached HEAD)"
        violations.append(
            f"HEAD moved from {before.head[:12]} to {after.head[:12]} on {branch_desc}"
        )
    if not violations:
        return None
    return (
        "gptme mutated git state directly ("
        + "; ".join(violations)
        + "). The resolver agent must leave branch and history changes to the orchestrator."
    )


def branch_for_issue(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}{issue_number}"


def build_agent_env(
    base_env: os._Environ[str] | dict[str, str], shim_dir: Path
) -> dict[str, str]:
    env = dict(base_env)
    for key in BLOCKED_AGENT_ENV_VARS:
        env.pop(key, None)
    path_parts = [str(shim_dir)]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def write_resolver_command_shims(shim_dir: Path) -> None:
    real_git = shutil.which("git")
    if not real_git:
        raise FileNotFoundError("git not found on PATH")
    allowed_subcommands = "|".join(READONLY_GIT_SUBCOMMANDS)
    _write_executable(
        shim_dir / "git",
        f"""#!/bin/sh
set -eu
# Find the real subcommand by skipping any global flags that appear before it.
# Single-arg flags (--no-pager, --bare, -P, etc.) are skipped as-is.
# Two-arg flags (-C <path>, -c <key=val>, --git-dir <path>, --work-tree <path>)
# consume the following token as their value.
subcmd=""
skip_next=0
for arg in "$@"; do
  if [ "$skip_next" -eq 1 ]; then
    skip_next=0
    continue
  fi
  case "$arg" in
    -C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--exec-path)
      skip_next=1
      continue
      ;;
    --*|-*)
      continue
      ;;
    *)
      subcmd="$arg"
      break
      ;;
  esac
done
case "$subcmd" in
  {allowed_subcommands})
    exec {real_git} "$@"
    ;;
  *)
    echo "gptme resolver blocked direct git command: git ${{subcmd:-<none>}}" >&2
    exit 1
    ;;
esac
""",
    )
    _write_executable(
        shim_dir / "gh",
        """#!/bin/sh
echo "gptme resolver blocked direct GitHub CLI usage; the orchestrator owns PR and comment actions." >&2
exit 1
""",
    )


def github_push_url(repo: str, auth_token: str) -> str:
    return f"https://x-access-token:{quote(auth_token, safe='')}@github.com/{repo}.git"


def push_branch(
    cwd: Path,
    branch: str,
    *,
    repo: str | None = None,
    auth_token: str | None = None,
) -> None:
    if auth_token and repo:
        git(
            [
                "push",
                "--force-with-lease",
                github_push_url(repo, auth_token),
                f"HEAD:refs/heads/{branch}",
            ],
            cwd=cwd,
        )
        return
    git(["push", "--force-with-lease", "origin", branch], cwd=cwd)


def commit_and_push(
    cwd: Path,
    branch: str,
    *,
    commit_message: str,
    repo: str | None = None,
    auth_token: str | None = None,
    author_name: str = "gptme-resolver",
    author_email: str = "gptme-resolver@users.noreply.github.com",
) -> None:
    """Commit all pending changes onto ``branch`` and push to origin."""
    git(["checkout", "-B", branch], cwd=cwd)
    git(["add", "-A"], cwd=cwd)
    # Configure the attempt commit author explicitly; the workflow runs as the
    # default `actions` user otherwise.
    git(
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            commit_message,
        ],
        cwd=cwd,
    )
    push_branch(cwd, branch, repo=repo, auth_token=auth_token)


def push_existing_head(
    cwd: Path,
    branch: str,
    *,
    repo: str | None = None,
    auth_token: str | None = None,
) -> None:
    """Preserve the current HEAD on ``branch`` without creating a new commit."""
    git(["checkout", "-B", branch], cwd=cwd)
    push_branch(cwd, branch, repo=repo, auth_token=auth_token)


def open_draft_pr(repo: str, issue_number: int, branch: str, summary: str) -> str:
    body = (
        f"{MARKER_COMMENT}\n\n"
        "Automated draft PR opened by the `gptme` resolver Action. This is a "
        "best-effort attempt — read the diff before merging.\n\n"
        f"Closes #{issue_number}\n\n"
        f"**Resolver summary:** {summary}\n"
    )
    try:
        raw = gh(
            [
                "pr",
                "create",
                "--repo",
                repo,
                "--draft",
                "--head",
                branch,
                "--title",
                f"[gptme-resolver] attempt for #{issue_number}",
                "--body",
                body,
            ]
        )
    except subprocess.CalledProcessError:
        # PR already exists for this branch (re-trigger). Return existing URL.
        raw = gh(["pr", "view", "--repo", repo, "--json", "url", "-q", ".url", branch])
    return raw.strip().splitlines()[-1] if raw.strip() else ""


def comment_on_issue(
    repo: str,
    issue_number: int,
    *,
    status: str,
    message: str,
    branch: str | None = None,
    pr_url: str | None = None,
) -> None:
    lines = [MARKER_COMMENT, ""]
    if status == "changes" and pr_url:
        lines.append(f"🛠️ gptme resolver opened a draft PR: {pr_url}\n\n{message}")
    elif status == "no_changes":
        lines.append(
            f"ℹ️ gptme resolver declined to make changes.\n\n**Reason:** {message}"
        )
        if branch:
            lines.append(f"\nPartial attempt preserved on branch `{branch}`.")
    else:
        lines.append(
            f"⚠️ gptme resolver failed to produce a usable result.\n\n"
            f"**Detail:** {message}"
        )
        if branch:
            lines.append(f"\nPartial attempt preserved on branch `{branch}`.")
    body = "\n".join(lines)
    post_issue_comment(repo, issue_number, body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--issue", type=int, required=True, help="issue number")
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path(__file__).parent / "prompts" / "issue-resolver.md",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory for session log + status artifacts.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="Repository working directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run gptme and print the proposed action without touching git or the issue.",
    )
    args = parser.parse_args(argv)

    issue = fetch_issue(args.repo, args.issue)
    template = args.prompt_file.read_text()
    prompt = render_prompt(template, repo=args.repo, issue=issue)
    baseline_state = capture_repo_state(args.workdir)
    auth_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    gptme_output, gptme_stderr = run_gptme(
        prompt, model=os.environ.get("GPTME_MODEL"), workdir=args.workdir
    )
    write_output(args.output_dir, "gptme-stdout.log", gptme_output or "")
    write_output(args.output_dir, "gptme-stderr.log", gptme_stderr or "")

    status, message = parse_status(gptme_output)
    write_output(
        args.output_dir,
        "resolver-status.json",
        json.dumps({"status": status, "message": message}, indent=2),
    )

    branch = branch_for_issue(issue.number)
    pr_url: str | None = None
    worktree_dirty = has_git_changes(args.workdir)
    repo_state_violation = detect_repo_state_violation(baseline_state, args.workdir)
    preserve_existing_head = (
        repo_state_violation is not None
        and capture_repo_state(args.workdir).head != baseline_state.head
    )

    if repo_state_violation is not None:
        status = "error"
        upstream_message = message
        message = repo_state_violation
        if upstream_message:
            message = f"{message} Upstream result: {upstream_message}"

    # gptme said "changes" — but only trust it if git actually sees changes.
    if status == "changes" and worktree_dirty:
        if args.dry_run:
            print(f"[dry-run] Would push {branch} and open draft PR: {message}")
            return 0
        commit_and_push(
            args.workdir,
            branch,
            commit_message=f"gptme-resolver attempt for #{issue.number}\n\n{message}",
            repo=args.repo,
            auth_token=auth_token,
        )
        pr_url = open_draft_pr(args.repo, issue.number, branch, message)
        comment_on_issue(
            args.repo,
            issue.number,
            status="changes",
            message=message,
            branch=branch,
            pr_url=pr_url,
        )
        print(f"Opened draft PR for #{issue.number} at {pr_url}")
        return 0

    # gptme said "changes" but worktree is clean — treat as a failed attempt.
    if status == "changes" and not worktree_dirty:
        status = "error"
        message = (
            "gptme reported changes but no files were modified. "
            f"Upstream message: {message}"
        )

    # no_changes / error: preserve partial work if present, including an
    # unsolicited direct commit that moved HEAD but left the worktree clean.
    partial_branch: str | None = None
    if worktree_dirty or preserve_existing_head:
        if args.dry_run:
            action = (
                "push partial attempt" if worktree_dirty else "preserve mutated HEAD"
            )
            print(f"[dry-run] Would {action} on {branch}")
        else:
            if worktree_dirty:
                commit_and_push(
                    args.workdir,
                    branch,
                    commit_message=(
                        f"gptme-resolver partial attempt for #{issue.number}\n\n"
                        f"Status: {status}\n\n{message}"
                    ),
                    repo=args.repo,
                    auth_token=auth_token,
                )
            else:
                push_existing_head(
                    args.workdir,
                    branch,
                    repo=args.repo,
                    auth_token=auth_token,
                )
            partial_branch = branch

    if args.dry_run:
        print(f"[dry-run] Would comment on issue — status={status}, message={message}")
        return 0

    comment_on_issue(
        args.repo,
        issue.number,
        status=status,
        message=message,
        branch=partial_branch,
    )
    print(f"Resolver finished for #{issue.number}: status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
