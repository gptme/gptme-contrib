"""Shared helpers for gptme GitHub Actions orchestrators.

Used by `scripts/github_hygiene/` (warning-only triage) and
`scripts/github_resolver/` (opt-in resolver). Both invoke `gh` to read an
issue, render a prompt, run `gptme` against it, and post a marker-tagged
comment back. This module owns the parts that are genuinely identical so
the two action scripts don't drift.

Intentionally **not** shared:

- `render_prompt(...)` — each Action injects different fields.
- `run_gptme(...)` — Actions pass different gptme flags (e.g. resolver runs
  inside a working directory and needs `--no-confirm`; hygiene runs
  read-only with `--tools read`).
- The marker constants (e.g. `<!-- gptme-issue-hygiene: v1 -->`) — each
  Action versions its marker independently.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_BODY_CHARS = 6000


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: list[str] = field(default_factory=list)


def gh(args: list[str], *, check: bool = True, timeout: int | None = 60) -> str:
    """Run `gh` CLI, return stdout. Raises CalledProcessError on non-zero."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )
    return result.stdout


def fetch_issue(repo: str, issue_number: int) -> Issue:
    """Fetch an issue and truncate its body to ``MAX_BODY_CHARS``."""
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


def has_marker_comment(repo: str, issue_number: int, marker: str) -> bool:
    """Return True if any comment on ``issue_number`` already contains ``marker``."""
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
        if marker in (comment.get("body") or ""):
            return True
    return False


def post_issue_comment(repo: str, issue_number: int, body: str) -> None:
    """Post a comment to ``repo#issue_number`` via the gh CLI."""
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
        timeout=60,
    )


def write_output(output_dir: Path, name: str, data: str) -> None:
    """Write ``data`` to ``output_dir/name``, creating the directory if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text(data)


__all__ = [
    "MAX_BODY_CHARS",
    "Issue",
    "gh",
    "fetch_issue",
    "has_marker_comment",
    "post_issue_comment",
    "write_output",
]
