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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MARKER_COMMENT = "<!-- gptme-issue-resolver: v1 -->"
STATUS_RE = re.compile(r"^RESOLVER_STATUS:\s*(changes|no_changes)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^RESOLVER_SUMMARY:\s*(.+?)\s*$", re.MULTILINE)
REASON_RE = re.compile(r"^RESOLVER_REASON:\s*(.+?)\s*$", re.MULTILINE)
MAX_BODY_CHARS = 6000
BRANCH_PREFIX = "gptme-resolver/issue-"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: list[str] = field(default_factory=list)


@dataclass
class ResolverOutcome:
    status: str  # "changes" | "no_changes" | "error"
    summary: str
    branch: str | None
    pr_url: str | None


def gh(args: list[str], *, check: bool = True) -> str:
    """Run `gh` CLI and return stdout."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=check)
    return result.stdout


def git(args: list[str], *, check: bool = True, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=check, cwd=cwd
    )
    return result.stdout


def fetch_issue(repo: str, issue_number: int) -> Issue:
    raw = gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,author,labels",
        ]
    )
    data = json.loads(raw)
    body = (data.get("body") or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n…(truncated)"
    return Issue(
        number=int(data["number"]),
        title=data["title"],
        body=body,
        author=(data.get("author") or {}).get("login", "unknown"),
        labels=[label["name"] for label in data.get("labels", [])],
    )


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


def run_gptme(prompt: str, *, model: str | None = None, workdir: Path) -> str:
    """Invoke gptme non-interactively. Fail soft: return empty on error."""
    cmd = ["gptme", "--non-interactive", "--no-confirm", "-"]
    if model:
        cmd.extend(["--model", model])
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            cwd=workdir,
        )
    except FileNotFoundError:
        sys.stderr.write("gptme not found on PATH; skipping resolver.\n")
        return ""
    if result.returncode != 0:
        sys.stderr.write(f"gptme failed (rc={result.returncode}):\n{result.stderr}\n")
    return result.stdout


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


def branch_for_issue(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}{issue_number}"


def commit_and_push(
    cwd: Path,
    branch: str,
    *,
    commit_message: str,
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
    git(["push", "--force-with-lease", "origin", branch], cwd=cwd)


def open_draft_pr(repo: str, issue_number: int, branch: str, summary: str) -> str:
    body = (
        f"{MARKER_COMMENT}\n\n"
        "Automated draft PR opened by the `gptme` resolver Action. This is a "
        "best-effort attempt — read the diff before merging.\n\n"
        f"Closes #{issue_number}\n\n"
        f"**Resolver summary:** {summary}\n"
    )
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
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo,
            "--body",
            body,
        ],
        check=True,
    )


def write_output(output_dir: Path, name: str, data: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text(data)


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

    gptme_output = run_gptme(
        prompt, model=os.environ.get("GPTME_MODEL"), workdir=args.workdir
    )
    write_output(args.output_dir, "gptme-stdout.log", gptme_output or "")

    status, message = parse_status(gptme_output)
    write_output(
        args.output_dir,
        "resolver-status.json",
        json.dumps({"status": status, "message": message}, indent=2),
    )

    branch = branch_for_issue(issue.number)
    pr_url: str | None = None

    # gptme said "changes" — but only trust it if git actually sees changes.
    if status == "changes" and has_git_changes(args.workdir):
        if args.dry_run:
            print(f"[dry-run] Would push {branch} and open draft PR: {message}")
            return 0
        commit_and_push(
            args.workdir,
            branch,
            commit_message=f"gptme-resolver attempt for #{issue.number}\n\n{message}",
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
    if status == "changes" and not has_git_changes(args.workdir):
        status = "error"
        message = (
            "gptme reported changes but no files were modified. "
            f"Upstream message: {message}"
        )

    # no_changes / error: preserve partial work if present.
    partial_branch: str | None = None
    if has_git_changes(args.workdir):
        if args.dry_run:
            print(f"[dry-run] Would push partial attempt to {branch}")
        else:
            commit_and_push(
                args.workdir,
                branch,
                commit_message=(
                    f"gptme-resolver partial attempt for #{issue.number}\n\n"
                    f"Status: {status}\n\n{message}"
                ),
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
