#!/usr/bin/env python3
# Upstreamed to gptme-contrib 2026-06-12 from Bob (TimeToBuildBob)'s
# scripts/github/greptile-merge-signal.py, so Alice + Bob share one score signal.
# Pairs with self-merge-check.py (the gate wires this in as a score floor) and the
# resolveReviewThread primitive (resolve-greptile-threads.py); the greploop pattern
# (greptileai/skills, MIT) is the broader fix→resolve→re-review→merge loop it serves.
"""Evaluate whether a Greptile summary comment is strong enough for fast-path merge use.

This is intentionally narrower than self-merge-check.py:
- It only inspects Greptile summary issue comments
- It requires an allowlisted bot login
- It requires "Safe to merge"
- It requires a minimum score threshold (default: 5/5)

The goal is to define a reusable call surface for monitoring fast paths without
weakening the main self-merge policy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_BOT_ALLOWLIST = ("greptile-apps[bot]", "greptile-apps")
DEFAULT_MIN_SCORE = 5
DISABLE_ENV = "GREPTILE_MERGE_SIGNAL_DISABLED"
BOT_ALLOWLIST_ENV = "GREPTILE_MERGE_SIGNAL_BOT_ALLOWLIST"
MIN_SCORE_ENV = "GREPTILE_MERGE_SIGNAL_MIN_SCORE"

SAFE_TO_MERGE_RE = re.compile(r"\bsafe to merge\b", re.IGNORECASE)
SUMMARY_MARKER_RE = re.compile(r"greptile summary", re.IGNORECASE)
SCORE_PATTERNS = (
    re.compile(r"confidence\s+score[^0-9]*(?P<score>[0-5])\s*/\s*5", re.IGNORECASE),
    re.compile(r"\bscore[^0-9]*(?P<score>[0-5])\s*/\s*5", re.IGNORECASE),
)
# Greptile's summary footer names the head its review actually covered, e.g.:
#   <sub>Reviews (8): Last reviewed commit: ["fix(...)"](https://github.com/o/r/commit/<sha>)
# Greptile edits the summary comment in place, so "latest summary" alone says
# nothing about WHICH head the score belongs to (gptme/gptme#3656: three
# commits postdated the score a handoff comment then attributed to the new
# head). The footer sha is the provenance that makes the score checkable.
REVIEWED_COMMIT_RE = re.compile(
    r"last\s+reviewed\s+commit.{0,400}?/commit/(?P<sha>[0-9a-f]{40})",
    re.IGNORECASE | re.DOTALL,
)
# Prefix comparisons shorter than this are not evidence of identity.
MIN_SHA_PREFIX_LEN = 7


@dataclass
class SignalResult:
    eligible: bool
    repo: str
    pr_number: int
    threshold: int
    signal_kind: str
    reason: str
    summary_found: bool = False
    safe_to_merge: bool = False
    score: int | None = None
    bot_login: str | None = None
    comment_id: int | None = None
    reviewed_at: str | None = None
    reviewed_commit: str | None = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--head-sha",
        default=None,
        help=(
            "Current PR head sha. When given and the summary's 'Last reviewed "
            "commit' footer names a different commit, the signal is ineligible "
            "(reason summary_stale_for_head): the score belongs to an older head."
        ),
    )
    parser.add_argument("pr_number", type=int)
    return parser.parse_args()


def _env_var_is_active(name: str) -> bool:
    """Return True if the env var is set to a truthy value ("1", "true", "yes", "on")."""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_bot_allowlist() -> set[str]:
    raw = os.environ.get(BOT_ALLOWLIST_ENV, "")
    if not raw.strip():
        return {login.lower() for login in DEFAULT_BOT_ALLOWLIST}
    return {entry.lower() for entry in raw.replace(",", " ").split() if entry.strip()}


def _parse_threshold() -> int:
    raw = os.environ.get(MIN_SCORE_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_SCORE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MIN_SCORE
    if 0 <= value <= 5:
        return value
    return DEFAULT_MIN_SCORE


def _run_gh(args: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _parse_json_stream(raw: str) -> list[Any]:
    values: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        value, end = decoder.raw_decode(raw, idx)
        values.append(value)
        idx = end
    return values


def _load_comments(repo: str, pr_number: int) -> list[dict[str, Any]]:
    raw = _run_gh(["api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"])
    if not raw.strip():
        return []
    comments: list[dict[str, Any]] = []
    for value in _parse_json_stream(raw):
        if isinstance(value, list):
            comments.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            comments.append(value)
    return comments


def _extract_score(body: str) -> int | None:
    for pattern in SCORE_PATTERNS:
        match = pattern.search(body)
        if match:
            return int(match.group("score"))
    return None


def _extract_reviewed_commit(body: str) -> str | None:
    match = REVIEWED_COMMIT_RE.search(body)
    return match.group("sha").lower() if match else None


def _sha_matches(reviewed: str, head: str) -> bool:
    """Prefix-match either way with a minimum-length guard (mirrors self-merge-check)."""
    reviewed = reviewed.strip().lower()
    head = head.strip().lower()
    if len(reviewed) < MIN_SHA_PREFIX_LEN or len(head) < MIN_SHA_PREFIX_LEN:
        return False
    return head.startswith(reviewed) or reviewed.startswith(head)


def _latest_allowlisted_summary(
    comments: list[dict[str, Any]], allowlist: set[str]
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for comment in comments:
        user = comment.get("user") or {}
        login = str(user.get("login") or "").strip()
        body = str(comment.get("body") or "")
        if login.lower() not in allowlist:
            continue
        if not SUMMARY_MARKER_RE.search(body):
            continue
        candidates.append(comment)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda comment: (
            str(comment.get("updated_at") or ""),
            str(comment.get("created_at") or ""),
        ),
    )


def evaluate_summary_signal(
    repo: str, pr_number: int, head_sha: str | None = None
) -> SignalResult:
    threshold = _parse_threshold()
    if _env_var_is_active(DISABLE_ENV):
        return SignalResult(
            eligible=False,
            repo=repo,
            pr_number=pr_number,
            threshold=threshold,
            signal_kind="disabled",
            reason="disabled",
        )

    allowlist = _parse_bot_allowlist()
    comments = _load_comments(repo, pr_number)
    latest_summary = _latest_allowlisted_summary(comments, allowlist)
    if latest_summary is None:
        return SignalResult(
            eligible=False,
            repo=repo,
            pr_number=pr_number,
            threshold=threshold,
            signal_kind="summary_comment",
            reason="no_allowlisted_summary_comment",
        )

    login = str((latest_summary.get("user") or {}).get("login") or "").strip()
    body = str(latest_summary.get("body") or "")
    score = _extract_score(body)
    reviewed_commit = _extract_reviewed_commit(body)
    safe_to_merge = bool(SAFE_TO_MERGE_RE.search(body))
    result = SignalResult(
        eligible=False,
        repo=repo,
        pr_number=pr_number,
        threshold=threshold,
        signal_kind="summary_comment",
        reason="",
        summary_found=True,
        safe_to_merge=safe_to_merge,
        score=score,
        bot_login=login or None,
        comment_id=latest_summary.get("id")
        if isinstance(latest_summary.get("id"), int)
        else None,
        reviewed_at=str(
            latest_summary.get("updated_at") or latest_summary.get("created_at") or ""
        )
        or None,
        reviewed_commit=reviewed_commit,
    )

    # Provenance gate first: a score for a head this PR no longer has is not a
    # score for this PR. Fail-open when the footer sha is absent/unparseable —
    # older summary formats carry no provenance, and the thread/category gates
    # in self-merge-check still apply.
    if head_sha and reviewed_commit and not _sha_matches(reviewed_commit, head_sha):
        result.reason = "summary_stale_for_head"
        return result

    if not safe_to_merge:
        result.reason = "safe_to_merge_missing"
        return result
    if score is None:
        result.reason = "score_missing"
        return result
    if score < threshold:
        result.reason = "score_below_threshold"
        return result

    result.eligible = True
    result.reason = "positive_summary_comment"
    return result


def main() -> int:
    args = _parse_args()
    result = evaluate_summary_signal(args.repo, args.pr_number, head_sha=args.head_sha)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
