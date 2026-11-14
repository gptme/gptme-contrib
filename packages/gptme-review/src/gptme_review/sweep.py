"""Portable sweep candidate selection and marker policy.

Hosts own GitHub access, mergeability, outage probes, consensus-upgrade
repos, and reactions. This module decides which open PRs still need a
review, given an explicit reviewer identity. It has no brain checkout,
default agent login, or quota collector.

The dispatch loop (subprocess reviewer, alerts, self-merge, REVIEW-key
budget) stays with the host. ``python -m gptme_review.sweep --dry-run``
lists candidates through a thin ``gh`` host so a second agent can verify
identity and scope without copying brain scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from gptme_review.core import blocking_shortfall, is_review_only_blocker

_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_BENIGN_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)(uv|poetry|cargo|yarn|pnpm|package-lock)\.lock(\.[a-z]+)?$"),
    re.compile(r"(^|/)Gemfile\.lock$"),
    re.compile(r"(^|/)composer\.lock$"),
    re.compile(r"(^|/)CHANGELOG(\.[a-z]+)?$"),
    re.compile(r"(^|/)HISTORY(\.[a-z]+)?$"),
    re.compile(r"(^|/)RELEASES?(\.[a-z]+)?$"),
)

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REACTION_QUEUED = "eyes"
MAX_DEGRADED_CONSENSUS_ATTEMPTS = 2
DEFAULT_MIN_AGE_SECS = 180
SUBSCRIPTION_DEFERS_LIMIT = 3
_PATCH_PARSE_CAP = 200_000
_DUP_JACCARD_FLOOR = 0.3
_DUP_MIN_SHARED_HUNKS = 2
_DUP_MIN_SHARED_HUNKS_TIGHT = 1
_DUP_SMALLER_HUNK_CEILING = 2


class SweepIdentity(NamedTuple):
    """GitHub login the reviewer posts as, plus its marker namespace.

    A ``NamedTuple`` so importlib-loaded hosts can construct it without a
    ``sys.modules`` entry. Factory :func:`sweep_identity` validates values.
    """

    login: str
    marker_prefix: str

    def marker_re(self) -> re.Pattern[str]:
        return re.compile(
            rf"<!-- {re.escape(self.marker_prefix)} (\{{.*?\}}) -->", re.DOTALL
        )

    def provisional_marker_re(self) -> re.Pattern[str]:
        return re.compile(
            rf"<!-- {re.escape(self.marker_prefix)}-publishing (\{{.*?\}}) -->",
            re.DOTALL,
        )

    def trigger_re(self) -> re.Pattern[str]:
        return re.compile(rf"(?im)^\s*@{re.escape(self.login)}\s+review\s*$")


def sweep_identity(login: str, marker_prefix: str) -> SweepIdentity:
    """Build an identity, failing closed on a malformed login or namespace."""
    if not _LOGIN_RE.match(login):
        raise ValueError(f"login={login!r} is not a GitHub login")
    if not _NAMESPACE_RE.match(marker_prefix):
        raise ValueError(f"marker_prefix={marker_prefix!r} must be a lowercase slug")
    return SweepIdentity(login=login, marker_prefix=marker_prefix)


@dataclass(frozen=True)
class SweepConfig:
    """Host-supplied scope. No package defaults for login, repos, or authors."""

    identity: SweepIdentity
    authors: tuple[str, ...] | None
    consensus_upgrade_repos: frozenset[str]
    min_age_secs: int = DEFAULT_MIN_AGE_SECS
    recall_boost_enabled: bool = False
    recall_boost_max_passes: int = 3
    subscription_defers_limit: int = SUBSCRIPTION_DEFERS_LIMIT
    trusted_associations: frozenset[str] = TRUSTED_ASSOCIATIONS


class SweepHost(Protocol):
    """GitHub and policy inputs the candidate loop does not own."""

    def list_open_prs(self, repo: str) -> list[dict] | None:
        """Open PRs, or None when listing failed (distinct from empty)."""

    def pr_comments(self, repo: str, number: int) -> list[dict] | None:
        """Comments, or None on failure; unavailable state must not spend a review."""

    def pr_mergeability(self, repo: str, number: int) -> dict: ...

    def openrouter_unavailable(self) -> bool: ...

    def add_reaction(self, repo: str, comment_id: int, content: str) -> bool: ...

    def fetch_pr_files(self, repo: str, number: int) -> list[str]: ...

    def fetch_pr_patch(self, repo: str, number: int) -> str: ...

    def is_review_only_blocker(self, mergeability: Mapping[str, object]) -> bool: ...


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _is_reviewer_comment(
    comment: Mapping[str, object], identity: SweepIdentity
) -> bool:
    """Marker namespaces are public; only the configured author owns state."""
    user = comment.get("user")
    return (
        isinstance(user, Mapping)
        and str(user.get("login") or "").casefold() == identity.login.casefold()
    )


def marker_payload(
    comments: Sequence[Mapping[str, object]], identity: SweepIdentity
) -> tuple[dict, dict | None]:
    """``(parsed marker JSON, the comment carrying it)`` — ``({}, None)`` if absent."""
    marker_re = identity.marker_re()
    for comment in comments:
        if not _is_reviewer_comment(comment, identity):
            continue
        match = marker_re.search(str(comment.get("body") or ""))
        if match:
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                return {}, dict(comment)
            return (parsed if isinstance(parsed, dict) else {}), dict(comment)
    return {}, None


def marker_state(
    comments: Sequence[Mapping[str, object]], identity: SweepIdentity
) -> tuple[str | None, str | None]:
    """``(reviewed head SHA, explicit requests served through timestamp)``."""
    marker_re = identity.marker_re()
    provisional_re = identity.provisional_marker_re()
    for comment in comments:
        if not _is_reviewer_comment(comment, identity):
            continue
        body = str(comment.get("body") or "")
        match = marker_re.search(body) or provisional_re.search(body)
        if match:
            try:
                state = json.loads(match.group(1))
                sha = str(state.get("sha") or "") or None
            except json.JSONDecodeError:
                return None, None
            served = state.get("served_through")
            if isinstance(served, str) and served:
                return sha, served
            if state.get("publishing_sha"):
                return sha, None
            updated = comment.get("updated_at")
            return sha, str(updated) if isinstance(updated, str) else None
    return None, None


def pending_review_request(
    comments: Sequence[Mapping[str, object]],
    marker_served_through: str | None,
    identity: SweepIdentity,
    *,
    trusted_associations: frozenset[str] = TRUSTED_ASSOCIATIONS,
) -> dict | None:
    """Newest unserved ``@<login> review`` from a trusted association."""
    newest: dict | None = None
    served_at = parse_ts(marker_served_through) if marker_served_through else None
    trigger_re = identity.trigger_re()
    marker_re = identity.marker_re()
    for comment in comments:
        body = str(comment.get("body") or "")
        if not trigger_re.search(body):
            continue
        if marker_re.search(body):
            continue
        if str(comment.get("author_association") or "") not in trusted_associations:
            continue
        created = str(comment.get("created_at") or "")
        created_at = parse_ts(created)
        if served_at is not None and created_at is not None and created_at <= served_at:
            continue
        if newest is None or created > str(newest.get("created_at") or ""):
            newest = dict(comment)
    return newest


def marker_findings_non_blocking(marker: Mapping[str, object]) -> bool:
    findings = marker.get("findings")
    if not isinstance(findings, list):
        return False
    if any(not isinstance(finding, dict) for finding in findings):
        return False
    dispositions = marker.get("dispositions")
    if not isinstance(dispositions, dict):
        dispositions = {}

    def _disposition(fp: object) -> str | None:
        entry = dispositions.get(fp) if isinstance(fp, str) else None
        if isinstance(entry, dict):
            reason = entry.get("reason")
            return str(reason) if reason else None
        return None

    return not blocking_shortfall(
        [
            {
                "fp": finding.get("fp"),
                "severity": finding.get("severity"),
                "isResolved": False,
                "replied": False,
                "superseded": False,
                "disposition": _disposition(finding.get("fp")),
            }
            for finding in findings
        ]
    )


def had_findings(marker: Mapping[str, object]) -> bool:
    score = marker.get("score")
    if isinstance(score, int) and score < 5:
        return True
    history = marker.get("history")
    if isinstance(history, list) and history:
        last = history[-1]
        if isinstance(last, dict):
            try:
                return int(last.get("findings") or 0) > 0
            except (TypeError, ValueError):
                return False
    return False


def recall_boost_passes(marker: Mapping[str, object], max_passes: int) -> int:
    history = marker.get("history")
    rounds = len(history) if isinstance(history, list) and history else 1
    cap = max(1, max_passes)
    if rounds < 2:
        return 1
    if rounds < 4:
        return min(2, cap)
    return cap


def needs_consensus_upgrade(
    marker: Mapping[str, object],
    head_sha: str,
    *,
    allow_non_blocking_findings: bool = False,
) -> bool:
    """Whether a current first pass needs merge-gate evidence."""
    marker_sha = str(marker.get("sha") or "")
    if not marker_sha or not head_sha.startswith(marker_sha):
        return False
    if marker.get("score") != 5 or marker.get("findings"):
        if not allow_non_blocking_findings:
            return False
        if not marker.get("findings") or not marker_findings_non_blocking(marker):
            return False
    consensus = marker.get("consensus")
    if not isinstance(consensus, dict):
        return True
    requested = consensus.get("requested")
    if not isinstance(requested, int):
        return True
    history = marker.get("history")
    history_rows = history if isinstance(history, list) else []
    if requested < 2:
        rounds_on_head = sum(
            1
            for entry in history_rows
            if isinstance(entry, dict) and entry.get("sha") == marker_sha
        )
        return rounds_on_head <= MAX_DEGRADED_CONSENSUS_ATTEMPTS
    answered = consensus.get("answered")
    if not isinstance(answered, int) or answered >= requested:
        return False
    if not consensus.get("failures"):
        return False
    attempts_on_head = 0
    for entry in history_rows:
        if not isinstance(entry, dict) or entry.get("sha") != marker_sha:
            continue
        passes = entry.get("passes")
        if isinstance(passes, int) and passes >= 2:
            attempts_on_head += 1
    return max(1, attempts_on_head) < MAX_DEGRADED_CONSENSUS_ATTEMPTS


def self_merge_eligible_from_marker(marker: Mapping[str, object]) -> bool:
    if not marker:
        return False
    if not marker_findings_non_blocking(marker):
        return False
    consensus = marker.get("consensus")
    if not isinstance(consensus, dict):
        return False
    requested = consensus.get("requested")
    answered = consensus.get("answered")
    min_agreement = consensus.get("min_agreement")
    min_agreement_requested = consensus.get("min_agreement_requested", min_agreement)
    if not isinstance(requested, int) or not isinstance(answered, int):
        return False
    if requested < 2 or answered < requested:
        return False
    if not isinstance(min_agreement, int) or min_agreement < 2:
        return False
    if (
        isinstance(min_agreement_requested, int)
        and min_agreement_requested >= 2
        and min_agreement < min_agreement_requested
    ):
        return False
    return True


def classify_all_failed_run(
    *,
    attempted: int,
    failures: int,
    rate_limit_failures: int,
    too_large_failures: int,
    capacity_failures: int = 0,
) -> str | None:
    if not attempted or failures != attempted:
        return None
    if attempted == 1:
        return "singleton"
    if rate_limit_failures == attempted:
        return "rate_limited"
    if too_large_failures == attempted or (
        rate_limit_failures + too_large_failures == attempted
    ):
        return "pr_local"
    if capacity_failures == attempted:
        return "capacity"
    return "systemic"


def is_benign_file(path: str) -> bool:
    if any(pat.search(path) for pat in _BENIGN_FILE_PATTERNS):
        return True
    if "/" not in path and "." not in path:
        return True
    return False


def hunks_from_patch(patch: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    current_file: str | None = None
    for line in patch.splitlines():
        match = _DIFF_FILE_RE.match(line)
        if match:
            current_file = match.group(1)
            out.setdefault(current_file, set())
            continue
        if current_file is None:
            continue
        match = _HUNK_RE.match(line)
        if not match:
            continue
        try:
            start = int(match.group(1))
        except ValueError:
            continue
        out[current_file].add(start)
    return out


def is_likely_duplicate(
    files_a: set[str],
    hunks_a: dict[str, set[int]],
    files_b: set[str],
    hunks_b: dict[str, set[int]],
) -> bool:
    shared_files = files_a & files_b
    non_benign_shared = {path for path in shared_files if not is_benign_file(path)}
    if not non_benign_shared:
        return False
    shared_hunks = 0
    union_hunks = 0
    for path in non_benign_shared:
        ha = hunks_a.get(path, set())
        hb = hunks_b.get(path, set())
        shared_hunks += len(ha & hb)
        union_hunks += len(ha | hb)
    if union_hunks == 0 or shared_hunks == 0:
        return False
    jaccard = shared_hunks / union_hunks
    total_a = sum(len(v) for v in hunks_a.values())
    total_b = sum(len(v) for v in hunks_b.values())
    smaller_hunks = min(total_a, total_b)
    larger_hunks = max(total_a, total_b)
    if shared_hunks >= _DUP_MIN_SHARED_HUNKS and jaccard >= _DUP_JACCARD_FLOOR:
        return True
    if (
        shared_hunks >= _DUP_MIN_SHARED_HUNKS_TIGHT
        and jaccard >= _DUP_JACCARD_FLOOR
        and smaller_hunks <= _DUP_SMALLER_HUNK_CEILING
        and larger_hunks <= _DUP_SMALLER_HUNK_CEILING
    ):
        return True
    return False


def detect_duplicate_pairs(
    entries: list[dict], repo: str, host: SweepHost
) -> list[dict]:
    if len(entries) < 2:
        return entries
    files_by_number: dict[int, set[str]] = {}
    for entry in entries:
        files = set(host.fetch_pr_files(repo, int(entry["number"])))
        if files:
            files_by_number[int(entry["number"])] = files
    by_number = {int(e["number"]): e for e in entries}
    candidate_pairs: list[tuple[int, int]] = []
    numbers = list(files_by_number.keys())
    for i, a in enumerate(numbers):
        for b in numbers[i + 1 :]:
            if files_by_number[a] & files_by_number[b]:
                candidate_pairs.append((a, b))
    if not candidate_pairs:
        return entries
    hunks_by_number: dict[int, dict[str, set[int]]] = {}
    for number in {n for pair in candidate_pairs for n in pair}:
        patch = host.fetch_pr_patch(repo, number)
        # Missing/binary-only patches supply no overlap evidence. Never invent
        # a shared hunk from file names alone.
        hunks_by_number[number] = hunks_from_patch(patch)
    for a, b in candidate_pairs:
        if is_likely_duplicate(
            files_by_number[a],
            hunks_by_number[a],
            files_by_number[b],
            hunks_by_number[b],
        ):
            older_num, newer_num = (a, b) if a < b else (b, a)
            newer = by_number[newer_num]
            older = by_number[older_num]
            if "likely_duplicate_of" in newer:
                continue
            newer["likely_duplicate_of"] = older_num
            newer["likely_duplicate_repo"] = older["repo"]
            newer["likely_duplicate_title"] = older.get("title", "") or ""
    return entries


def select_candidates(
    repo: str,
    config: SweepConfig,
    host: SweepHost,
    now: datetime,
    *,
    dry_run: bool = False,
    subscription_defers: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict] | None:
    """Open PRs in ``repo`` that still need a review, or None if listing failed."""
    prs = host.list_open_prs(repo)
    if not isinstance(prs, list):
        return None

    identity = config.identity
    out: list[dict] = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        head = str(pr.get("headRefOid") or "")
        if not head:
            continue
        if subscription_defers is not None:
            pr_key = f"{repo}#{pr['number']}"
            entry = subscription_defers.get(pr_key) or {}
            count_raw = entry.get("count")
            count = count_raw if isinstance(count_raw, int) else 0
            if entry.get("head") == head and count >= config.subscription_defers_limit:
                continue

        comments = host.pr_comments(repo, int(pr["number"]))
        if comments is None:
            continue
        seen, marker_served_through = marker_state(comments, identity)
        marker, marker_comment = marker_payload(comments, identity)
        requested = pending_review_request(
            comments,
            marker_served_through,
            identity,
            trusted_associations=config.trusted_associations,
        )

        if (
            marker.get("outage_single_pass")
            and seen
            and head.startswith(seen)
            and requested is None
            and host.openrouter_unavailable()
        ):
            continue

        review_can_upgrade = marker.get("engine") in {"llm", "agent", "subscription"}
        authors = config.authors
        if authors is not None:
            login = ((pr.get("author") or {}).get("login")) or ""
            if login not in authors and requested is None:
                continue
        updated = parse_ts(str(pr.get("updatedAt") or ""))
        if (
            updated
            and (now - updated).total_seconds() < config.min_age_secs
            and requested is None
        ):
            continue

        upgrade_allowed = (
            repo in config.consensus_upgrade_repos
            and requested is None
            and review_can_upgrade
        )
        consensus_upgrade = upgrade_allowed and needs_consensus_upgrade(marker, head)
        if (
            upgrade_allowed
            and not consensus_upgrade
            and needs_consensus_upgrade(marker, head, allow_non_blocking_findings=True)
        ):
            consensus_upgrade = host.is_review_only_blocker(
                host.pr_mergeability(repo, int(pr["number"]))
            )
        needs_non_blocking_upgrade = not upgrade_allowed and needs_consensus_upgrade(
            marker, head, allow_non_blocking_findings=True
        )
        if (
            seen
            and head.startswith(seen)
            and requested is None
            and not consensus_upgrade
            and not needs_non_blocking_upgrade
        ):
            continue
        item = {"repo": repo, **pr}
        item["same_head_upgrade"] = bool(
            needs_non_blocking_upgrade and seen and head.startswith(seen)
        )
        if requested is not None:
            item["requested_by"] = ((requested.get("user") or {}).get("login")) or "?"
        if consensus_upgrade:
            item["consensus_upgrade"] = True
        item["is_fix_iteration"] = bool(
            seen and not head.startswith(seen) and had_findings(marker)
        )
        if (
            config.recall_boost_enabled
            and item["is_fix_iteration"]
            and requested is None
            and not consensus_upgrade
            and marker.get("engine") == "llm"
        ):
            boost = recall_boost_passes(marker, config.recall_boost_max_passes)
            if boost > 1:
                item["recall_boost"] = boost
        item["reviewed_sha"] = seen
        item["never_attempted"] = marker_comment is None
        item["all_findings_non_blocking"] = (
            marker_comment is not None and marker_findings_non_blocking(marker)
        )
        unreviewable = marker.get("unreviewable")
        if (
            isinstance(unreviewable, dict)
            and requested is None
            and (u_sha := str(unreviewable.get("sha") or ""))
            and head.startswith(u_sha)
        ):
            item["unreviewable"] = unreviewable
        item["marker_comment_id"] = (marker_comment or {}).get("id")
        item["request_comment_id"] = (requested or {}).get("id")
        if not dry_run:
            if item["marker_comment_id"] and item["is_fix_iteration"]:
                host.add_reaction(repo, int(item["marker_comment_id"]), REACTION_QUEUED)
            if item["request_comment_id"]:
                host.add_reaction(
                    repo, int(item["request_comment_id"]), REACTION_QUEUED
                )
        out.append(item)

    detect_duplicate_pairs(out, repo, host)
    return out


def _gh_json(args: list[str], timeout: int = 60) -> object | None:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed: object = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return parsed


def _gh_paginated_list(args: list[str], timeout: int = 60) -> list[dict] | None:
    parsed = _gh_json([*args, "--paginate", "--slurp"], timeout=timeout)
    if not isinstance(parsed, list):
        return None
    if all(isinstance(page, list) for page in parsed):
        items: list[object] = [item for page in parsed for item in page]
    else:
        items = list(parsed)
    return [item for item in items if isinstance(item, dict)]


class GhCliHost:
    """Default host for the dry-run CLI: live ``gh``, no alerts or self-merge."""

    def list_open_prs(self, repo: str) -> list[dict] | None:
        payload = _gh_json(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,headRefOid,isDraft,updatedAt,title,author,"
                "additions,deletions,changedFiles",
                "--limit",
                "50",
            ]
        )
        return payload if isinstance(payload, list) else None

    def pr_comments(self, repo: str, number: int) -> list[dict] | None:
        return _gh_paginated_list(
            ["api", f"repos/{repo}/issues/{number}/comments"], timeout=60
        )

    def pr_mergeability(self, repo: str, number: int) -> dict:
        payload = _gh_json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "mergeable,statusCheckRollup",
            ],
            timeout=30,
        )
        return payload if isinstance(payload, dict) else {}

    def openrouter_unavailable(self) -> bool:
        return False

    def add_reaction(self, repo: str, comment_id: int, content: str) -> bool:
        proc = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/comments/{comment_id}/reactions",
                "-f",
                f"content={content}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        return proc.returncode == 0

    def fetch_pr_files(self, repo: str, number: int) -> list[str]:
        payload = _gh_json(
            ["api", f"repos/{repo}/pulls/{number}/files?per_page=100"], timeout=30
        )
        if not isinstance(payload, list):
            return []
        return [
            str(item.get("filename"))
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        ]

    def fetch_pr_patch(self, repo: str, number: int) -> str:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(number), "--repo", repo],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if proc.returncode != 0:
            return ""
        return str(proc.stdout)[:_PATCH_PARSE_CAP]

    def is_review_only_blocker(self, mergeability: Mapping[str, object]) -> bool:
        return is_review_only_blocker(mergeability)


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item for item in re.split(r"[\s,]+", raw.strip()) if item)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--login",
        required=True,
        help="GitHub login this reviewer posts as (no package default)",
    )
    parser.add_argument(
        "--marker-namespace",
        required=True,
        help="Marker slug unique to this reviewer (lowercase)",
    )
    parser.add_argument("--repos", nargs="+", required=True)
    parser.add_argument(
        "--authors",
        nargs="+",
        default=None,
        help="Only these PR authors; omit or pass 'any' to disable the filter",
    )
    parser.add_argument(
        "--consensus-upgrade-repos",
        default="",
        help="Comma/whitespace list of repos allowed a same-head consensus upgrade",
    )
    parser.add_argument("--min-age-secs", type=int, default=DEFAULT_MIN_AGE_SECS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="List candidates without reactions (default: on for this CLI)",
    )
    parser.add_argument(
        "--live-reactions",
        action="store_true",
        help="Allow queued-reaction writes; dry-run stays the default",
    )
    args = parser.parse_args(argv)
    identity = sweep_identity(args.login, args.marker_namespace)
    authors: tuple[str, ...] | None
    if args.authors is None or args.authors == ["any"]:
        authors = None
    else:
        authors = tuple(args.authors)
    config = SweepConfig(
        identity=identity,
        authors=authors,
        consensus_upgrade_repos=frozenset(_split_csv(args.consensus_upgrade_repos)),
        min_age_secs=args.min_age_secs,
    )
    host = GhCliHost()
    now = datetime.now(UTC)
    dry_run = not args.live_reactions
    pending: list[dict] = []
    listing_failures: list[str] = []
    for repo in args.repos:
        found = select_candidates(repo, config, host, now, dry_run=dry_run)
        if found is None:
            listing_failures.append(repo)
            continue
        pending.extend(found)
    if listing_failures:
        print(
            f"LISTING FAILED for {len(listing_failures)}/{len(args.repos)} "
            f"repo(s): {', '.join(listing_failures)}",
            file=sys.stderr,
        )
    if len(listing_failures) == len(args.repos):
        return 1
    if not pending:
        print("no dispatchable PRs")
        return 0
    for pr in pending:
        flags = []
        if pr.get("requested_by"):
            flags.append(f"requested_by={pr['requested_by']}")
        if pr.get("consensus_upgrade"):
            flags.append("consensus_upgrade")
        if pr.get("never_attempted"):
            flags.append("never_attempted")
        if pr.get("unreviewable"):
            flags.append("unreviewable")
        extra = f" ({', '.join(flags)})" if flags else ""
        print(f"{pr['repo']}#{pr['number']} {pr.get('headRefOid', '')[:12]}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
