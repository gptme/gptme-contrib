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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("pr_number", type=int)
    return parser.parse_args()


def _env_enabled(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() not in {"1", "true", "yes", "on"}


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


def evaluate_summary_signal(repo: str, pr_number: int) -> SignalResult:
    threshold = _parse_threshold()
    if not _env_enabled(DISABLE_ENV):
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
    )

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
    result = evaluate_summary_signal(args.repo, args.pr_number)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
