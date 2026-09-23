#!/usr/bin/env python3
"""Canonical transient-401 / auth-death classifier shared across all surfaces.

Every 401-reactive surface an agent runs — an operator loop, a worker lane, a
dispatched subagent, an autonomous-run trajectory scan — needs the *same*
answer to one question: "is this output/stderr/trajectory a transient
auth-death?" When that classifier is duplicated it drifts, and the drift is
silent: one copy misses a marker the other catches, so a real auth death is
misclassified as a completed run (or vice versa). This module is the single
source of truth so surfaces stop drifting.

Origin: ported from Bob's `scripts/lib/auth_401.py` (task `harden-401-auth-
everywhere`, ErikBjare/bob#968), where a transient 401 surfaced in three places
in one day — the operator loop froze, the worker lane blacklisted real work,
and a subagent died mid-task — because three copies of the classifier disagreed.
Generalized here to be agent-agnostic (no agent-specific paths) with the one
genuine policy difference between agents exposed as **configuration**: whether a
403 means auth-death or quota (see `include_403` below).

## Two layers, not one

This module is the **text → signature** layer: it answers "does this raw text
carry a 401/auth signature?". It does NOT own the *policy* over already-
classified failure reasons (e.g. "which `failure_reason` enum values count as an
outage that must be escalated"). That reason-set policy is a separate, thinner
layer an agent keeps locally (Alice's `lib/auth_outage.py` is one such set).
The two are complementary: this module produces/recognises the signal, the
reason-set decides what to do about an already-named reason.

## The broad-vs-envelope split (both preserved)

- `is_transient_401(text)` — **broad** matcher, no size gate. Matches a 401/auth
  signature anywhere in an arbitrary string (stderr, a captured error, a single
  trajectory line). Broad on purpose: for the worker-output case the tiny-output
  size gate (`is_auth_death`) is what prevents false positives, so the matcher
  only has to *recognise* the death.
- `is_trajectory_auth_death(text, include_403=...)` — **envelope** matcher for a
  full session trajectory. Keys only on the *structured* JSON error envelope, so
  a large successful trajectory that merely *discusses* 401s in prose/diffs
  (e.g. this very auth-hardening work) is not misclassified into a fleet-wide
  stale-marker.

## The 403 policy (the one real per-agent difference)

Whether a 403 is auth-death or quota differs by agent, and the difference is
real, not accidental:
  - **Bob**: 403 is a *quota* signal (`quota_403_signal.sh`), NOT a stale
    credential. Default here (`include_403=False`) keeps 403 out of the
    envelope auth-death lane.
  - **Alice**: a 403 can be an org-level entitlement death —
    `oauth_org_not_allowed` / "organization has disabled Claude subscription
    access" — which is the same silent-dark outcome as a 401 (2026-08-24/25).
    Pass `include_403=True` to fold the 403 envelope into auth-death.

The *broad* matcher (`is_transient_401`) always includes a bare 403, because
behind the worker size gate a 403 that instantly killed a tiny run is an
auth/entitlement death regardless of agent; the split only matters for the
prose-safe envelope matcher.

Public API:
  - `is_transient_401(text)`
  - `is_trajectory_auth_death(text, include_403=False)`
  - `is_auth_death(output_file, max_bytes)`

CLI (for bash surfaces — operator loops, spawn scripts, autonomous runners):
  - `--classify-stdin`            read stdin; exit 0 iff a 401 signature present
  - `--classify-file PATH`        size-gated auth-death check (worker output)
  - `--classify-trajectory PATH`  prose-safe JSON-envelope auth-death check
  - `--include-403`               (with --classify-trajectory) fold 403 envelope
                                  into auth-death (Alice's org-disabled policy)
  - `--max-bytes N`               override the size floor for --classify-file

Exit 0 = transient-401/auth-death detected; exit 1 = not (or unreadable).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Auth-failure markers emitted by the Claude Code / run.sh path on a dead auth
# token, plus the trajectory-JSON markers a session emits. Matched
# case-insensitively. Kept broad on purpose — for the worker file case the
# tiny-output gate is what prevents false positives, so this only has to
# recognise the death.
AUTH_DEATH_PATTERNS = [
    r"\b401\b",
    r"\b403\b",
    r"unauthorized",
    r"invalid bearer token",
    r"authentication_error",
    r"authentication_failed",  # trajectory JSON: "error":"authentication_failed"
    r"invalid[_ ]api[_ ]key",
    r"oauth\b.{0,40}\bexpired",
    r"oauth\b.{0,40}\b(fail|error|invalid)",
    r"please run /login",
    r"credit balance is too low",  # billing/auth-adjacent instant death
    r"disabled.*subscription",  # subscription access revoked (e.g. payment bounced)
]

_AUTH_RE = re.compile("|".join(AUTH_DEATH_PATTERNS), re.IGNORECASE)

# Trajectory-JSON auth-death markers. Unlike `is_transient_401` (which matches a
# bare "401"/"403" anywhere — only safe for a tiny worker-output file behind the
# size gate), this keys on the *structured* JSON error envelope a CC/gptme
# trajectory emits on an auth death. A full autonomous trajectory routinely
# *discusses* 401s in prose/diffs, so the broad matcher would misclassify it
# into a fleet-wide stale-marker. These 401-scoped patterns are always active.
TRAJECTORY_AUTH_DEATH_PATTERNS = [
    r'"error_status"\s*:\s*401',
    r'"error"\s*:\s*"authentication_failed"',
    r'"(?:error|type)"\s*:\s*"authentication_error"',
]

# 403-in-envelope markers, opt-in via `include_403`. Bob keeps these OUT (403 is
# his quota signal); Alice folds them IN (403 = org entitlement death). Scoped
# to the structured field so a prose "403" is never enough on its own.
TRAJECTORY_AUTH_403_PATTERNS = [
    r'"error_status"\s*:\s*403',
    r"oauth_org_not_allowed",
]

_TRAJECTORY_AUTH_RE = re.compile(
    "|".join(TRAJECTORY_AUTH_DEATH_PATTERNS), re.IGNORECASE
)
_TRAJECTORY_AUTH_403_RE = re.compile(
    "|".join(TRAJECTORY_AUTH_DEATH_PATTERNS + TRAJECTORY_AUTH_403_PATTERNS),
    re.IGNORECASE,
)

# Smallest real worker output observed (2026-06-24) was ~46KB, so a 2KB floor
# has a >20x safety margin against false positives from completed workers.
DEFAULT_MAX_BYTES = 2000


def is_transient_401(text: str) -> bool:
    """Return True iff `text` carries a 401 / auth-failure signature.

    No size gate — use this to classify a captured stderr/error string or a
    single trajectory line. For the size-gated worker-output case use
    `is_auth_death`.
    """
    return bool(_AUTH_RE.search(text))


def is_trajectory_auth_death(text: str, include_403: bool = False) -> bool:
    """Auth-death detection for a full session trajectory.

    Matches only the structured JSON error envelope a trajectory emits on an
    auth failure — NOT bare mentions of "401"/"403" in prose or diffs — so a
    large successful or unrelated-crash trajectory that merely discusses auth is
    not misclassified into a fleet-wide stale-marker.

    `include_403` folds the 403 envelope into auth-death (Alice's org-disabled
    entitlement policy). Default False keeps 403 out (Bob's quota policy).
    """
    regex = _TRAJECTORY_AUTH_403_RE if include_403 else _TRAJECTORY_AUTH_RE
    return bool(regex.search(text))


def is_auth_death(output_file: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Return True iff the output is small AND carries an auth-failure signature.

    The worker case: a worker that died instantly on a 401 produces a tiny
    output that should not burn an attempt. Both conditions are required so a
    large, genuinely-completed worker whose diff mentions "401" is not flagged.
    """
    try:
        data = output_file.read_bytes()
    except (OSError, ValueError):
        return False
    if len(data) > max_bytes:
        return False
    text = data.decode("utf-8", errors="replace")
    return is_transient_401(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transient-401 / auth-death classifier"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--classify-stdin",
        action="store_true",
        help="Read stdin and exit 0 iff it carries a 401/auth-failure signature",
    )
    group.add_argument(
        "--classify-file",
        metavar="PATH",
        help="Size-gated auth-death check on a worker output file",
    )
    group.add_argument(
        "--classify-trajectory",
        metavar="PATH",
        help="Prose-safe auth-death check on a full session trajectory "
        "(matches the JSON error envelope only; exit 0 iff auth death)",
    )
    parser.add_argument(
        "--include-403",
        action="store_true",
        help="With --classify-trajectory: fold the 403 envelope into auth-death "
        "(org-disabled entitlement policy); default treats 403 as quota",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Max output size to still count as an instant death (default {DEFAULT_MAX_BYTES})",
    )
    args = parser.parse_args(argv)

    if args.classify_stdin:
        if is_transient_401(sys.stdin.read()):
            print("transient-401", file=sys.stderr)
            return 0
        return 1

    if args.classify_trajectory:
        try:
            text = Path(args.classify_trajectory).read_text(
                encoding="utf-8", errors="replace"
            )
        except (OSError, ValueError):
            return 1
        if is_trajectory_auth_death(text, include_403=args.include_403):
            print("trajectory-auth-death", file=sys.stderr)
            return 0
        return 1

    if is_auth_death(Path(args.classify_file), args.max_bytes):
        print("auth-death", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
