"""GitHub issue hygiene orchestrator.

Reads a newly-opened issue, runs `gptme` against a narrow prompt, and posts at
most one warning-only comment marked with an HTML idempotency marker.

Warning-only by design: no close policy, no label mutations, no edits.
See: knowledge/research/2026-04-23-opencode-github-hygiene-agents.md (Bob's repo)

Usage (inside a GitHub Action):

    python -m issue_hygiene \
        --repo "$GITHUB_REPOSITORY" \
        --issue "$ISSUE_NUMBER" \
        --prompt-file prompts/issue-hygiene.md

Environment:
    GH_TOKEN     — token with `issues: write` scope
    GPTME_MODEL  — optional model override (defaults to env default)

The script is idempotent: if a comment containing MARKER is already present on
the issue, it exits 0 without calling gptme or posting anything. This matches
OpenCode's `duplicate-issues.yml` pattern (HTML marker for replayable runs).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = "<!-- gptme-issue-hygiene: v1 -->"
SKIP_TOKEN = "NO_ISSUES"
MAX_RECENT_ISSUES = 15
MAX_BODY_CHARS = 6000


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: list[str]


@dataclass
class RecentIssue:
    number: int
    title: str


def gh(args: list[str], *, check: bool = True) -> str:
    """Run `gh` CLI, return stdout. Raises CalledProcessError on non-zero."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
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


def fetch_recent_issues(
    repo: str, *, limit: int = MAX_RECENT_ISSUES, exclude: int | None = None
) -> list[RecentIssue]:
    raw = gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit + 1),
            "--json",
            "number,title",
        ]
    )
    rows = json.loads(raw)
    out: list[RecentIssue] = []
    for row in rows:
        num = int(row["number"])
        if exclude is not None and num == exclude:
            continue
        out.append(RecentIssue(number=num, title=row["title"]))
        if len(out) >= limit:
            break
    return out


def already_commented(repo: str, issue_number: int) -> bool:
    raw = gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "comments",
        ]
    )
    data = json.loads(raw)
    for comment in data.get("comments", []):
        if MARKER in (comment.get("body") or ""):
            return True
    return False


def render_prompt(
    template: str,
    *,
    repo: str,
    issue: Issue,
    recent: list[RecentIssue],
) -> str:
    recent_block = "\n".join(f"- #{r.number}: {r.title}" for r in recent) or "(none)"
    labels_block = ", ".join(issue.labels) or "(none)"
    body = issue.body or "(empty body)"
    return template.format(
        repo=repo,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_author=issue.author,
        issue_labels=labels_block,
        issue_body=body,
        recent_issues=recent_block,
    )


def run_gptme(prompt: str, *, model: str | None = None) -> str:
    """Invoke `gptme` in non-interactive mode with the rendered prompt.

    We pipe the prompt via stdin and use `--non-interactive` so the action
    does not block waiting on stdin TTY.
    """
    cmd = ["gptme", "--non-interactive", "--tools", "read"]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("gptme timed out after 300s; skipping comment.\n")
        return ""
    if result.returncode != 0:
        # Fail soft: print stderr but return empty to suppress commenting.
        sys.stderr.write(f"gptme failed (rc={result.returncode}):\n{result.stderr}\n")
        return ""
    return result.stdout.strip()


def build_comment(verdict: str) -> str:
    preface = (
        "**Automated issue hygiene check** — warning-only, posted by "
        "[gptme](https://github.com/gptme/gptme). "
        "If anything below is wrong, just reply and a human will sort it out.\n\n"
    )
    return f"{MARKER}\n\n{preface}{verdict.strip()}\n"


def post_comment(repo: str, issue_number: int, body: str) -> None:
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
        check=True,
        timeout=60,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--issue", type=int, required=True, help="issue number")
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path(__file__).parent / "prompts" / "issue-hygiene.md",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render prompt and print gptme output without posting a comment.",
    )
    args = parser.parse_args(argv)

    if already_commented(args.repo, args.issue):
        print(f"Already commented on {args.repo}#{args.issue}; skipping.")
        return 0

    issue = fetch_issue(args.repo, args.issue)
    recent = fetch_recent_issues(args.repo, exclude=issue.number)
    template = args.prompt_file.read_text()
    prompt = render_prompt(template, repo=args.repo, issue=issue, recent=recent)

    verdict = run_gptme(prompt, model=os.environ.get("GPTME_MODEL"))
    if not verdict:
        print("gptme returned empty output; skipping.")
        return 0
    if SKIP_TOKEN in verdict:
        print("gptme reported NO_ISSUES; skipping.")
        return 0

    body = build_comment(verdict)
    if args.dry_run:
        print("--- dry run, would post: ---")
        print(body)
        return 0

    post_comment(args.repo, args.issue, body)
    print(f"Posted hygiene comment on {args.repo}#{args.issue}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
