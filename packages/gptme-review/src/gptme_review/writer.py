"""Per-PR review writer: marker, publish, thread, disposition, render, checkout and preflight helpers.

Writer-port slices 2a–2d (Bob's knowledge/technical-designs/2026-11-20-ai-review-writer-port.md).
Code moved as-is from Bob's ``scripts/github/ai-review.py``; review behaviour is
unchanged.

**I/O and identity resolve at call time through** ``_io``. A host calls
:func:`bind_io` with a namespace providing ``MARKER_NAMESPACE``,
``MARKER_PREFIX``, ``PROVISIONAL_MARKER_PREFIX``, ``REVIEWER_IDENTITY`` (an
object with ``login``) and the I/O seams (``run``, ``gh_json``,
``gh_paginated_list``; ``_find_all_marker_comments`` / ``_delete_marker_comment``
/ ``fetch_review_comments`` / ``head_path_exists`` optionally override the writer's own) and uses the functions it returns. Bob
binds a view of his script module, so existing
``monkeypatch.setattr(script, "run", ...)`` seams still reach calls made inside
these helpers. A peer binds :func:`default_io` built from its own
:class:`~gptme_review.sweep.SweepIdentity`. There is no package-default identity:
the module-level functions are unbound and raise instead of parsing or posting
under another reviewer's markers.
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import UTC, datetime
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Protocol
from urllib.parse import quote

from bobutils.shell import gh_json, gh_paginated_list

from gptme_review import lifecycle
from gptme_review.core import (
    UNTIL_DRY_MAX_PASSES,
    ReviewConfig,
    ReviewDeadline,
    ReviewError,
    ReviewRoute,
    ReviewTarget,
    agreement_dissent,
    blocking_shortfall,
    confidence_rationale,
    confidence_score,
    consensus_degraded,
    consensus_lost_jobs,
    consensus_lost_passes,
    drop_unverified_absence_findings,
    fetch_file_contents,
    fetch_file_preload,
    fetch_submodule_context,
    filter_suppressed,
    is_summary_only_finding,
    matches_entry,
    merge_verdict,
    normalize_finding_class,
    normalize_severity,
    partition_posting_findings,
    rank_changed_paths_by_hunks,
    repo_family,
    review,
    select_aspects,
    suppression_entry,
)
from gptme_review.shard import (
    SHARD_CONCURRENCY,
    Shard,
    merge_shard_findings,
    merge_shard_usage,
    shard_context_note,
    sharding_note,
    split_file_sections,
)
from gptme_review.sweep import SweepIdentity, _is_reviewer_comment


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


class _IO:
    """Resolve seams against a host namespace on every attribute access.

    Names the namespace does not provide fall back to the writer's own
    functions bound to the same host, so a peer only supplies identity and I/O.
    """

    def __init__(self, ns: object | None = None, fallback: dict | None = None):
        self._ns = ns
        self._fallback = fallback or {}

    def __getattr__(self, name: str):
        ns = self.__dict__.get("_ns")
        if ns is None:
            raise RuntimeError(
                "gptme_review.writer has no host bound; use bind_io(namespace)"
            )
        try:
            return getattr(ns, name)
        except AttributeError:
            fallback = self.__dict__["_fallback"]
            if name in fallback:
                return fallback[name]
            raise


#: Unbound: calling a module-level helper directly raises. Hosts call bind_io().
_io = _IO()


def bind_io(namespace: object) -> SimpleNamespace:
    """Return the writer's functions bound to *namespace*, without global state.

    Each call clones the module's functions over a private globals dict whose
    ``_io`` resolves against *namespace* at call time. Two hosts (or two loads of
    one host script, as test suites do) therefore never see each other's seams.
    """
    scope = dict(globals())
    bound: dict[str, FunctionType] = {}
    for name, obj in globals().items():
        if isinstance(obj, FunctionType) and obj.__module__ == __name__:
            if name in {"bind_io", "default_io"}:
                continue
            clone = FunctionType(
                obj.__code__, scope, obj.__name__, obj.__defaults__, obj.__closure__
            )
            clone.__kwdefaults__ = obj.__kwdefaults__
            clone.__doc__ = obj.__doc__
            clone.__qualname__ = obj.__qualname__
            bound[name] = clone
    scope.update(bound)
    scope["_io"] = _IO(namespace, bound)
    return SimpleNamespace(**bound)


def default_io(
    identity: SweepIdentity, host: DefaultWriterHost | None = None
) -> SimpleNamespace:
    """A peer host's namespace: this module's I/O plus *identity*'s markers.

    *host* supplies the run ledger and subscription engine hooks used by the
    engine call helpers; ``None`` means :class:`DefaultWriterHost`.
    """
    ns = identity.marker_prefix
    return SimpleNamespace(
        REVIEWER_IDENTITY=identity,
        WRITER_HOST=host if host is not None else DefaultWriterHost(),
        MAINTAINER_COMMANDS=("review",),
        review=review,
        fetch_file_contents=fetch_file_contents,
        _ORIGINAL_FETCH_FILE_CONTENTS=fetch_file_contents,
        fetch_file_preload=fetch_file_preload,
        fetch_submodule_context=fetch_submodule_context,
        drop_unverified_absence_findings=drop_unverified_absence_findings,
        run=run,
        gh_json=gh_json,
        gh_paginated_list=gh_paginated_list,
        MARKER_NAMESPACE=ns,
        MARKER_PREFIX=f"<!-- {ns}",
        PROVISIONAL_MARKER_PREFIX=f"<!-- {ns}-publishing",
        CHECKOUT_ROOT=Path(
            os.environ.get("GPTME_REVIEW_CHECKOUT_ROOT", "/tmp/worktrees")
        ),
    )


def _find_all_marker_comments(repo: str, number: int) -> list[dict]:
    """All AI-review marker comments on this PR (should normally be 0 or 1)."""
    comments = _io.gh_paginated_list(["api", f"repos/{repo}/issues/{number}/comments"])
    return [
        c
        for c in comments
        if isinstance(c, dict)
        and _io.MARKER_PREFIX in (c.get("body") or "")
        # Marker namespaces are public: anyone can paste one. Only the
        # reviewer's own comments carry state, and only those may be deleted.
        and _is_reviewer_comment(c, _io.REVIEWER_IDENTITY)
    ]


def _delete_marker_comment(repo: str, comment_id: int) -> None:
    """Delete a single issue comment — used to clean up duplicate markers."""
    proc = _io.run(
        [
            "gh",
            "api",
            "--method",
            "DELETE",
            f"repos/{repo}/issues/comments/{comment_id}",
        ]
    )
    if proc.returncode != 0:
        print(
            f"warn: could not delete stale marker comment {comment_id}: {proc.stderr[-100:]}",
            file=sys.stderr,
        )
    else:
        print(f"deleted stale duplicate marker comment {comment_id}")


def find_marker_comment(repo: str, number: int) -> dict | None:
    """The existing AI-review comment on this PR, if any.

    Matched on the marker prefix ALONE — not on the SHA — so that a re-review
    edits the same comment instead of stacking a new one per commit.

    If concurrent reviewer runs left multiple marker comments, the stale
    duplicates are deleted here and the most-recently-updated one is returned.
    This is the cleanup half of the upsert-race fix; the prevention half is the
    re-check of `existing` immediately before the POST/PATCH decision.
    """
    all_markers = _io._find_all_marker_comments(repo, number)
    if not all_markers:
        return None
    if len(all_markers) == 1:
        found: dict = all_markers[0]
        return found
    # Multiple markers — concurrent reviewer race. Keep the most recently
    # updated one (matches the selection logic in the merge gate's GraphQL path)
    # and delete the rest so neither this session nor the gate reads stale data.
    print(
        f"warn: {repo}#{number} has {len(all_markers)} marker comments — cleaning duplicates",
        file=sys.stderr,
    )
    all_markers.sort(
        key=lambda c: c.get("updated_at") or c.get("created_at") or "",
        reverse=True,
    )
    keeper: dict = all_markers[0]
    for stale in all_markers[1:]:
        _io._delete_marker_comment(repo, stale["id"])
    return keeper


def _marker_re() -> re.Pattern[str]:
    return re.compile(
        rf"<!-- {re.escape(_io.MARKER_NAMESPACE)}(?:-publishing)? (\{{.*?\}}) -->",
        re.DOTALL,
    )


def marker_state(body: str) -> dict:
    """Machine-readable state embedded in the marker comment."""
    m = _marker_re().search(body or "")
    if not m:
        # Pre-upsert comments used `<!-- bob-ai-review head-sha: X -->`.
        legacy = re.search(
            rf"<!-- {re.escape(_io.MARKER_NAMESPACE)} head-sha: (\S+) -->", body or ""
        )
        if legacy:
            return {"sha": legacy.group(1), "history": []}
        return {}
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def append_review_history(
    previous: dict, entry: dict, consensus: dict | None
) -> list[dict]:
    """Record requested passes for both publication paths and legacy recovery.

    Only the latest legacy entry can be classified from the previous marker's
    consensus. Preserve older unknown rounds instead of guessing their mode.
    """
    history = [dict(h) for h in previous.get("history") or [] if isinstance(h, dict)]
    if previous.get("sha") == entry["sha"]:
        previous_passes = (previous.get("consensus") or {}).get("requested", 1)
        if history and history[-1].get("sha") == entry["sha"]:
            if "passes" not in history[-1]:
                history[-1]["passes"] = previous_passes
        elif isinstance(previous_passes, int) and previous_passes >= 2:
            # Even a marker without history proves its current consensus ran.
            history.append(
                {"sha": entry["sha"], "passes": previous_passes, "inferred": True}
            )
    return history + [{**entry, "passes": (consensus or {}).get("requested", 1)}]


def build_marker_state(
    *,
    head_sha: str,
    score: int | None,
    engine: str,
    findings: Sequence[Mapping[str, object]],
    kept: Mapping[int, Mapping[str, str] | str] | None = None,
    consensus: dict | None = None,
    history: Sequence[dict] | None = None,
    suppressed: Sequence[dict] | None = None,
    advisories: Sequence[Mapping[str, object]] | None = None,
    submodule_only: bool = False,
    preload_coverage: Mapping[str, int] | None = None,
) -> dict:
    """The machine-readable half of the marker comment.

    Extracted so the pre-PR publish path (`ai-review-publish-local.py`) writes a
    marker byte-identical in shape to the sweep's. `sha` keeps its exact
    meaning: `ai-review-sweep.py` skips a PR when marker sha == head, and the
    self-merge gate (gptme/gptme-contrib#1388) and PM dispatch (#1390) both read
    it. Adding a key is safe; moving or retyping one is not.

    `kept` overrides the fresh fingerprint for any finding reconciled to a live
    thread, and may override its severity when the kept thread renders one. The
    gate builds its disposable set from threads, so publishing a fresh identity
    for a finding we deliberately did not re-post names a `(fingerprint,
    severity)` pair that exists nowhere on the PR and can never be disposed.
    Publish the identity actually on the diff.

    `consensus` is omitted (not nulled) when no consensus stage ran — read a
    MISSING key as "unknown", never as "full consensus".
    """
    kept = kept or {}
    indexed_findings = [(i, dict(f)) for i, f in enumerate(findings)]
    posted_findings = [
        (i, f) for i, f in indexed_findings if not is_summary_only_finding(f)
    ]
    classified_advisories = [
        f for _, f in indexed_findings if is_summary_only_finding(f)
    ]
    all_advisories = [*classified_advisories, *(advisories or [])]
    posted_entries = []
    for i, f in posted_findings:
        kept_identity = kept.get(i)
        if isinstance(kept_identity, Mapping):
            fp = str(kept_identity.get("fp") or suppression_entry(f)["fp"])
            severity = str(
                kept_identity.get("severity") or normalize_severity(f.get("severity"))
            )
        else:
            # Backward-compatible for callers that only supply the historical
            # fingerprint override.
            fp = str(kept_identity or suppression_entry(f)["fp"])
            severity = normalize_severity(f.get("severity"))
        posted_entries.append({"fp": fp, "severity": severity})
    state: dict = {
        "sha": head_sha,
        "score": score,
        "engine": engine,
        # Additive: the self-merge gate treats ``score: null`` as an
        # abstention only when this is true (gitlink-only pointer bump,
        # gptme-cloud#850). Explicit false is a real review whose in-scope
        # findings were empty or policy-dropped — not "not reviewed"
        # (contrib#1579, #1580). Older markers omit the key; omission must
        # fail closed.
        "submodule_only": bool(submodule_only),
        "findings": posted_entries,
    }
    if posted_entries:
        # Keep the established compact ``findings`` wire shape intact while
        # retaining the new class needed to measure per-class outcomes later.
        # Fingerprints are already the marker's finding identity.
        state["finding_classes"] = {
            entry["fp"]: normalize_finding_class(f.get("class"))
            for entry, (_, f) in zip(posted_entries, posted_findings, strict=True)
        }
        # Current execution evidence, keyed by the identity actually published.
        # Original thread provenance stays in its immutable inline root.
        provenance = {
            entry["fp"]: f["provenance"]
            for entry, (_, f) in zip(posted_entries, posted_findings, strict=True)
            if f.get("provenance")
        }
        if provenance:
            state["finding_provenance"] = provenance
    if all_advisories:
        state["advisories"] = [
            {
                **suppression_entry(f),
                "severity": normalize_severity(f.get("severity")),
                "class": normalize_finding_class(f.get("class")),
                "source": str(f.get("aspect") or "model"),
            }
            for f in all_advisories
        ]
    if consensus:
        state["consensus"] = consensus
    state["history"] = list(history or [])[-10:]
    state["suppressed"] = list(suppressed or [])
    if preload_coverage is not None:
        for key in ("files_in_diff", "files_preloaded", "files_truncated"):
            state[key] = int(preload_coverage.get(key, 0))
    return state


#: Delimits the human-readable half of an "I cannot review this" notice, so it
#: can be rewritten in place instead of accumulating one paragraph per sweep.
def unreviewable_note_markers() -> tuple[str, str]:
    ns = _io.MARKER_NAMESPACE
    return f"<!-- {ns}-unreviewable -->", f"<!-- /{ns}-unreviewable -->"


def _unreviewable_note_re() -> re.Pattern[str]:
    start, end = unreviewable_note_markers()
    return re.compile(re.escape(start) + ".*?" + re.escape(end), re.DOTALL)


def unreviewable_note(reason: str, detail: str) -> str:
    """The visible half of the notice — what a human reads on the PR."""
    start, end = unreviewable_note_markers()
    return (
        f"{start}\n"
        f"⚠️ **This PR could not be reviewed automatically** ({reason}).\n\n"
        f"> {detail.strip()[-300:]}\n\n"
        "GitHub will not serve a diff this large over the API, so the reviewer "
        "has nothing to read. This head will not be retried; **push a smaller "
        "head, or split the PR, and the next sweep will pick it up "
        "automatically.**\n"
        f"{end}"
    )


def render_unreviewable_body(existing_body: str, reason: str, detail: str) -> str:
    """Merge the notice into *existing_body* without destroying a past review.

    A PR can have been reviewed at an earlier, smaller head. That review's
    rendered body is the only human-readable record of it, so the notice is
    spliced in beside it rather than over it — and any previous notice is
    replaced, never stacked.
    """
    note = unreviewable_note(reason, detail)
    body = _marker_re().sub("", existing_body or "").strip()
    note_re = _unreviewable_note_re()
    if note_re.search(body):
        return note_re.sub(lambda _: note, body, count=1).strip()
    return f"{body}\n\n{note}".strip() if body else note


def render_review_event_body(publication_id: str) -> str:
    """Invisible, verdict-free body for the native GitHub review event.

    The durable issue comment is the one textual verdict surface, so the review
    event that carries the inline threads gets an HTML-only publication marker
    instead of a second, immutable score/finding summary. A review event is only
    published when there is at least one inline comment to attach to it — see
    ``publish_review`` for why an empty one is never posted.

    Same-account publication deliberately stays ``COMMENT``. GitHub does not
    allow a PR author to approve or request changes on their own PR; a future
    distinct App identity can choose those events without changing this body.
    """
    return f"<!-- automated-ai-review-publication:{publication_id} -->"


def marker_body(body: str, state: Mapping[str, object]) -> str:
    """Attach one compact machine-readable state marker to a summary body."""
    return f"{body}\n\n{_io.MARKER_PREFIX} {json.dumps(dict(state))} -->"


def provisional_marker_body(body: str, state: Mapping[str, object]) -> str:
    """Attach recoverable state that completed-review consumers ignore."""
    return f"{body}\n\n{_io.PROVISIONAL_MARKER_PREFIX} {json.dumps(dict(state))} -->"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def completed_marker_serves(
    comment: Mapping[str, object], full_sha: str, required_served_through: object
) -> bool:
    """Whether a racing publisher completed this head after our run began."""
    state = marker_state(str(comment.get("body") or ""))
    sha = str(state.get("sha") or "")
    if not sha or not full_sha.startswith(sha):
        return False
    completed = _parse_timestamp(state.get("served_through"))
    required = _parse_timestamp(required_served_through)
    return completed is not None and required is not None and completed >= required


def provisional_marker_state(
    previous: Mapping[str, object],
    final: Mapping[str, object],
    *,
    head_sha: str,
    publication_id: str,
    publishing_at: str,
) -> dict:
    """Fail-closed state written before the native review is submitted.

    No ``sha``, ``score``, ``findings`` or ``consensus`` key is copied: those
    mean a completed review to downstream consumers. Bookkeeping that protects
    retry correctness is retained, notably ``auto_resolved`` (a resolution may
    land even if the later review POST fails) and the prior ``served_through``
    watermark (touching this provisional comment must not acknowledge a review
    request).
    """
    history = previous.get("history")
    suppressed = final.get("suppressed")
    auto_resolved = final.get("auto_resolved")
    state: dict[str, object] = {
        "publishing_sha": head_sha,
        "publishing_at": publishing_at,
        "publication_id": publication_id,
        "history": list(history) if isinstance(history, list) else [],
        "suppressed": list(suppressed) if isinstance(suppressed, list) else [],
        "auto_resolved": (
            list(auto_resolved) if isinstance(auto_resolved, list) else []
        ),
    }
    served = previous.get("served_through")
    if isinstance(served, str) and served:
        state["served_through"] = served
    if final.get("inband") is True:
        state["inband"] = True
    return state


def render_provisional_body(head_sha: str) -> str:
    """Human-visible state while review threads are being published."""
    return (
        "## 🤖 AI code review\n\n"
        f"⏳ Publishing the review for `{head_sha}`. The durable verdict will "
        "replace this notice after the native review and inline threads succeed."
    )


def _publication_lock(repo: str, number: int):
    """Acquire the host-local per-PR publication lock.

    Review analysis may run concurrently; only the short GitHub mutation phase
    is serialized. The marker's provisional state remains the cross-process
    crash record, while this lock prevents two local sweep/local-publish
    processes from interleaving POST/PATCH/review calls.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo).strip("-") or "repo"
    lock = Path(f"/tmp/{_io.MARKER_NAMESPACE}-publish-{safe}-{number}.lock").open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    return lock


def record_unreviewable(
    repo: str,
    number: int,
    head_sha: str,
    existing: dict | None,
    state: dict,
    detail: str,
    reason: str = "diff_too_large",
) -> bool:
    """Mark *head_sha* as one the reviewer cannot read, in the marker comment.

    Persisted in the marker rather than in a local ledger because that is this
    system's whole state model — ``pending_review_request`` calls it "derived,
    not stored" — and a second, local source of truth would drift out of sync
    with the PR exactly when it mattered.

    Crucially this does NOT set ``sha``. ``sha`` means "reviewed at", and the
    sweep, the self-merge gate (contrib#1388) and PM dispatch (#1390) all read
    it that way; claiming a review happened would let an unreviewed 477-file PR
    satisfy a merge gate. ``unreviewable`` is a separate, additive key that
    consumers unaware of it simply ignore — they keep seeing this PR as
    unreviewed, which is the truth.
    """
    state_out = dict(state)
    state_out["unreviewable"] = {
        "sha": head_sha,
        "reason": reason,
        "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    body = (
        render_unreviewable_body((existing or {}).get("body", ""), reason, detail)
        + f"\n\n{_io.MARKER_PREFIX} {json.dumps(state_out)} -->"
    )

    if existing:
        proc = _io.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{existing['id']}",
                "-f",
                f"body={body}",
            ],
        )
    else:
        proc = _io.run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{number}/comments",
                "-f",
                f"body={body}",
            ],
        )
    if proc.returncode != 0:
        # Non-fatal: the review has already failed, and failing to record WHY
        # only costs a retry next sweep — the pre-existing behaviour.
        print(
            f"warn: could not record unreviewable marker ({proc.stderr[-200:]})",
            file=sys.stderr,
        )
        return False
    print(f"recorded {repo}#{number} as unreviewable at {head_sha} ({reason})")
    return True


class ShadowHandle(Protocol):
    """A started shadow review that ``main`` waits for before exiting."""

    def join(self, timeout: float) -> None: ...


class DefaultWriterHost:
    """No-op hooks: what a peer host without Bob's modules gets."""

    def log_run(
        self,
        *,
        repo: str,
        number: int,
        sha: str,
        model: str,
        usage: Mapping[str, object],
        wall_s: float,
        findings: list[dict],
        error: str | None = None,
    ) -> None:
        """Record one primary OpenRouter review run."""

    def start_shadow(
        self, *, repo: str, number: int, sha: str, prompt: str
    ) -> ShadowHandle | None:
        """Start a never-posting shadow review; return its handle, or None."""
        return None

    def overlap(self, diff: str, repo: str) -> object | None:
        """Return an existing-module overlap hit for ``diff``, or None."""
        return None

    def overlap_note(self, hit: object) -> str:
        return ""

    def overlap_finding(self, hit: object) -> dict | None:
        return None

    def verify(
        self,
        findings: list[dict],
        diff: str,
        *,
        model: str,
        api_key: str,
        deadline: ReviewDeadline | None,
        blocked: bool,
        repo: str,
        pr: int,
        head_sha: str,
        file_contents: Mapping[str, str],
    ) -> tuple[list[dict], list[dict], dict]:
        """Keep every finding: the verifier's own conservative failure policy."""
        return (
            list(findings),
            [],
            {"confirmed": len(findings), "dropped": 0, "errors": 0, "host": "none"},
        )

    def append_advisories(
        self, repo: str, number: int, sha: str, advisories: list
    ) -> tuple[int, list[str]]:
        """Mirror published advisories to an audit ledger; return (rows, diagnostics)."""
        return 0, []

    def extra_static_checks(
        self, diff: str, touched_files: Collection[str]
    ) -> list[dict]:
        """Host-specific mechanical findings added to static preflight."""
        return []

    def subscription_review_call(
        self,
        args: argparse.Namespace,
        pr: Mapping[str, object],
        *,
        diff: str,
        contents: dict[str, str],
        note: str,
        label: str = "",
    ) -> tuple[list[dict], dict]:
        """One subscription-panel review of ``diff``; unsupported by default."""
        raise ReviewError("the subscription engine is not available on this host")

    def route_override(self, args: argparse.Namespace) -> tuple[str, str] | None:
        """Return ``(route, why)`` to override an auto-resolved llm route, or None."""
        return None


# ---------------------------------------------------------------------------
# Two-phase publication (writer-port slice 3b)
#
# ``run``, ``find_marker_comment``, ``_publication_lock`` and
# ``resolve_review_thread`` resolve through ``_io`` so a host's patches reach
# the mutation calls; the advisory audit ledger is a ``host`` hook.
# ---------------------------------------------------------------------------


def _native_review_published(
    repo: str, number: int, full_sha: str, review_body: str
) -> bool:
    """Read GitHub's durable POST receipt before retrying a publication.

    Do not use ``gh_paginated_list`` here: it returns [] on errors, which would
    turn an unreadable receipt into permission to duplicate inline threads.
    """
    result = _io.run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{number}/reviews",
            "--paginate",
            "--slurp",
        ],
        env={**os.environ, "GH_API_CACHE_TTL": "0"},
    )
    if result.returncode != 0:
        raise ReviewError(f"cannot check prior native review: {result.stderr[-300:]}")
    try:
        pages = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ReviewError("cannot decode prior native reviews") from exc
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        raise ReviewError("unexpected prior native reviews response")
    reviews = [review for page in pages for review in page]
    if not all(isinstance(review, dict) for review in reviews):
        raise ReviewError("unexpected prior native review entry")
    return any(
        str(review.get("body") or "").strip() == review_body
        and review.get("commit_id") == full_sha
        and review.get("state") in {"COMMENTED", "DISMISSED"}
        and _is_reviewer_comment(review, _io.REVIEWER_IDENTITY)
        for review in reviews
    )


def publish_review(
    *,
    repo: str,
    number: int,
    full_sha: str,
    summary_body: str,
    final_state: Mapping[str, object],
    previous_state: Mapping[str, object],
    inline: Sequence[Mapping[str, object]],
    existing: Mapping[str, object] | None,
    stale_threads: Sequence[str] = (),
    dry_run: bool = False,
    host: DefaultWriterHost | None = None,
) -> bool:
    """Publish one durable summary and one verdict-free native review.

    Order is the contract:

    1. POST/PATCH a provisional, fail-closed summary.
    2. Resolve superseded machine threads and submit the native COMMENT review.
    3. PATCH the same summary to its completed verdict.

    A failure at either mutation boundary leaves a provisional marker with no
    completed SHA/score, so the sweep retries and every merge consumer fails
    closed. ``served_through`` advances only in the final state supplied by the
    caller, never merely because the summary comment's ``updated_at`` moved.

    ``host.append_advisories`` mirrors published advisories to an audit ledger;
    ``None`` means :class:`DefaultWriterHost`, which records nothing.
    """
    if host is None:
        host = DefaultWriterHost()
    head_sha = full_sha[:12]
    prior_publication_id = previous_state.get("publication_id")
    prior_publishing_sha = str(previous_state.get("publishing_sha") or "")
    # A receipt proves only the payload from that publication, not a fresh
    # analysis of the same commit. Bind retries to the exact inline payload.
    inline_digest = hashlib.sha256(
        json.dumps(list(inline), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    recovering = (
        isinstance(prior_publication_id, str)
        and bool(prior_publication_id)
        and bool(prior_publishing_sha)
        and head_sha.startswith(prior_publishing_sha)
        and previous_state.get("publishing_inline_sha256") == inline_digest
    )
    publication_id = str(prior_publication_id) if recovering else secrets.token_hex(8)
    publishing_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    previous_for_provisional = dict(previous_state)
    # Preserve the derived watermark when upgrading a legacy completed marker.
    # Otherwise the first failed two-phase publication would make every old
    # trigger comment look unserved merely because the old marker predates the
    # explicit ``served_through`` field.
    legacy_updated_at = (existing or {}).get("updated_at")
    if (
        "served_through" not in previous_for_provisional
        and previous_for_provisional.get("sha")
        and isinstance(legacy_updated_at, str)
        and legacy_updated_at
    ):
        previous_for_provisional["served_through"] = legacy_updated_at
    provisional_state = provisional_marker_state(
        previous_for_provisional,
        final_state,
        head_sha=head_sha,
        publication_id=publication_id,
        publishing_at=publishing_at,
    )
    provisional_state["publishing_inline_sha256"] = inline_digest
    provisional_body = provisional_marker_body(
        render_provisional_body(head_sha), provisional_state
    )

    completed_state = dict(final_state)
    completed_state["publication_id"] = publication_id
    completed_body = marker_body(summary_body, completed_state)
    review_body = render_review_event_body(publication_id)

    if dry_run:
        print(completed_body)
        print("\n--- publication order ---")
        print("1. provisional summary comment")
        if inline:
            print("2. native COMMENT review and inline threads")
        else:
            print("2. no native review event (no inline comments)")
        print("3. final PATCH of the same summary comment")
        print("\n--- review event body ---")
        print(review_body)
        print(f"\n--- would post {len(inline)} inline comment(s) ---")
        print(f"--- would resolve {len(stale_threads)} superseded thread(s) ---")
        return True

    lock = _io._publication_lock(repo, number)
    try:
        # Re-read after acquiring the lock. If another local publisher changed
        # the comment while this run was analysing, its result wins; overwriting
        # it would create duplicate native reviews or resurrect stale state.
        current = _io.find_marker_comment(repo, number)
        initial_body = str((existing or {}).get("body") or "")
        required_served_through = final_state.get("served_through")
        if existing is None and current is not None:
            if completed_marker_serves(current, full_sha, required_served_through):
                print("skip: another run completed this review while publishing")
                return True
            print(
                "error: an incomplete or older review marker appeared while publishing",
                file=sys.stderr,
            )
            return False
        if existing is not None:
            if current is None:
                print(
                    "error: review marker disappeared while publishing", file=sys.stderr
                )
                return False
            if (
                current.get("id") != existing.get("id")
                or str(current.get("body") or "") != initial_body
            ):
                if completed_marker_serves(current, full_sha, required_served_through):
                    print("skip: another run completed this review while publishing")
                    return True
                print(
                    "error: review marker changed without completing this run",
                    file=sys.stderr,
                )
                return False

        summary_id: int | None = None
        if current is not None:
            raw_id = current.get("id")
            summary_id = raw_id if isinstance(raw_id, int) else None
            if summary_id is None:
                print(
                    "error: existing review marker has no numeric id", file=sys.stderr
                )
                return False
            provisional = _io.run(
                [
                    "gh",
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/comments/{summary_id}",
                    "-f",
                    f"body={provisional_body}",
                ]
            )
        else:
            provisional = _io.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/issues/{number}/comments",
                    "-f",
                    f"body={provisional_body}",
                    "--jq",
                    ".id",
                ]
            )
            if provisional.returncode == 0:
                try:
                    summary_id = int(provisional.stdout.strip())
                except ValueError:
                    summary_id = None
        if provisional.returncode != 0 or summary_id is None:
            print(
                f"error posting provisional summary: {provisional.stderr[-300:]}",
                file=sys.stderr,
            )
            return False

        if stale_threads:
            resolved = sum(
                _io.resolve_review_thread(tid, repo) for tid in stale_threads
            )
            print(f"resolved {resolved} superseded inline finding thread(s)")

        native_posted = False
        if recovering and inline:
            try:
                native_posted = _native_review_published(
                    repo, number, full_sha, review_body
                )
            except ReviewError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return False

        if not inline:
            # Nothing to attach. A marker-only COMMENT review renders in the PR
            # timeline as a review event with no visible content and no threads
            # — noise Erik flagged on gptme/gptme-contrib#1599 (2026-09-03).
            # The durable summary comment is already the verdict surface, so
            # skip the event rather than publishing an empty container. This is
            # also the retry seam: a run whose final PATCH failed re-publishes
            # nothing here, so no duplicate event can appear.
            print("no inline comments; skipping empty native review event")
        elif native_posted:
            print("native review already published; skipping duplicate inline threads")
        else:
            payload = {
                "commit_id": full_sha,
                # Same-account publication must remain advisory. A distinct App
                # identity may map rubric outcomes to formal review events later.
                "event": "COMMENT",
                "body": review_body,
                "comments": list(inline),
            }
            review = _io.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/pulls/{number}/reviews",
                    "--input",
                    "-",
                ],
                input=json.dumps(payload),
            )
            if review.returncode != 0:
                print(
                    f"error posting native review: {review.stderr[-300:]}",
                    file=sys.stderr,
                )
                return False
            print(f"posted native review with {len(inline)} inline comment(s)")

        completed_state["completed_at"] = (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        completed_body = marker_body(summary_body, completed_state)
        final = _io.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{summary_id}",
                "-f",
                f"body={completed_body}",
                "--jq",
                ".html_url",
            ]
        )
        if final.returncode != 0:
            print(
                f"error finalizing review summary: {final.stderr[-300:]}",
                file=sys.stderr,
            )
            return False
        print(f"finalized: {final.stdout.strip()}")
        # Both production and local-artifact reviews publish here. Capture the
        # advisory evidence before a later review replaces the mutable summary.
        # This audit mirror is not merge authority: its failure cannot undo an
        # already published review or cause duplicate publication on retry.
        advisories = completed_state.get("advisories")
        if isinstance(advisories, list) and advisories:
            try:
                count, diagnostics = host.append_advisories(
                    repo, number, full_sha, advisories
                )
                print(f"advisory ledger: appended {count} row(s)")
                for diagnostic in diagnostics:
                    print(f"warning: advisory ledger: {diagnostic}", file=sys.stderr)
            except Exception as exc:  # best-effort host hook, not merge authority
                print(
                    f"warning: advisory ledger capture failed: {exc}; "
                    f"run ai-review-dispose.py {repo} {number} --reconcile",
                    file=sys.stderr,
                )
        return True
    finally:
        lock.close()


# ---------------------------------------------------------------------------
# Threads, suppression and dispositions (writer-port slice 2b)
#
# Identity-derived markers resolve through ``_io.MARKER_NAMESPACE`` at call
# time; the patched seams (``gh_json``, ``fetch_review_comments``, ``run``)
# resolve through ``_io`` so a host's patches reach calls made in here.
# ---------------------------------------------------------------------------


def _inline_fp_re() -> re.Pattern[str]:
    return re.compile(
        rf"<!-- {re.escape(_io.MARKER_NAMESPACE)}-fp (\{{.*?\}}) -->", re.DOTALL
    )


# Stamped by `ai-review-dispose.py` into a REPLY that deliberately settles a
# finding — a false positive argued down, a won't-fix, an accepted trade-off.
#
# Why it exists: `resolution_dismissals` cannot tell a deliberate close from
# this reviewer's own bookkeeping, because everything posts as
# `TimeToBuildBob`, so it leans on the `auto_resolved` ledger. That ledger has a
# blind spot — on a PR whose marker predates it, the backfill below adopts EVERY
# existing resolution as ours, which swallows a disposition an agent made
# minutes earlier (observed on gptme/gptme#3468: three dispositions adopted, and
# only the weaker rejection-phrase path rescued two of them).
#
# This marker is the provenance the login cannot provide: it appears only when
# an agent explicitly answered and closed a finding. It is therefore trusted
# over the backfill — see `dismissed_findings`. Distinct from INLINE_FP_PREFIX
# on purpose: that identifies a FINDING, this identifies an ANSWER to one.
def disposition_prefix() -> str:
    return f"<!-- {_io.MARKER_NAMESPACE}-disposition "


def _disposition_re() -> re.Pattern[str]:
    return re.compile(
        rf"<!-- {re.escape(_io.MARKER_NAMESPACE)}-disposition (\{{.*?\}}) -->",
        re.DOTALL,
    )


def iter_disposition_stamps(
    comments: Sequence[Mapping[str, object]],
    *,
    diagnostics: list[str] | None = None,
) -> Iterable[tuple[Mapping[str, object], dict]]:
    """Every source reply/stamp in order, before collapsing duplicate findings."""
    for comment in comments:
        if type(comment.get("in_reply_to_id")) is not int:
            continue
        body = str(comment.get("body") or "")
        payloads = _disposition_re().findall(body)
        if diagnostics is not None and body.count(disposition_prefix()) > len(payloads):
            diagnostics.append(
                f"reply {comment.get('id')}: malformed disposition stamp"
            )
        for raw in payloads:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                if diagnostics is not None:
                    diagnostics.append(
                        f"reply {comment.get('id')}: invalid disposition JSON"
                    )
                continue
            if isinstance(parsed, dict) and parsed.get("fp"):
                yield comment, parsed
            elif diagnostics is not None:
                diagnostics.append(
                    f"reply {comment.get('id')}: disposition has no fingerprint"
                )


def disposed_fingerprints(comments: Sequence[Mapping[str, object]]) -> set[str]:
    """Fingerprints an agent explicitly disposed of, read off the PR itself.

    Derived state rather than a local ledger, for the same reason the marker is:
    the PR is the source of truth, so this survives a crashed run and a rerun
    from a different session. Only *replies* count (`in_reply_to_id` set), so a
    finding root can never be mistaken for its own disposition.
    """
    return {str(stamp["fp"]) for _, stamp in iter_disposition_stamps(comments)}


def all_dispositions(comments: Sequence[Mapping[str, object]]) -> dict[str, dict]:
    """Every disposition recorded on the PR, keyed by finding fingerprint.

    Unlike `disposed_fingerprints` (which only returns the fp set), this keeps
    the whole payload — reason, and (when present) ``sha`` / ``disputes`` — so
    a re-render can show a reader *why* a finding is settled, not just that it
    is. Read off the PR itself, same as `disposed_fingerprints`: a crashed run
    or a different session sees the same state. Later replies win on a
    duplicate fingerprint (should not happen — `dispose()` is idempotent — but
    last-write-wins is the least surprising tiebreak if it ever does).
    """
    out: dict[str, dict] = {}
    for _, stamp in iter_disposition_stamps(comments):
        out[str(stamp["fp"])] = stamp
    return out


#: Phrases that mark a reply as *rejecting* a finding rather than accepting it.
#:
#: Deliberately an allow-list, and deliberately conservative: an unrecognised
#: reply leaves the finding standing. Treating any reply as a dismissal would
#: quietly suppress a real bug the moment someone answered "will fix later" and
#: then didn't — the exact way a merge-gating signal must never fail. A missed
#: dismissal only costs one repeated comment.
REJECTION_PHRASES = (
    "not a bug",
    "not a real",
    "not an issue",
    "false positive",
    "incorrect finding",
    "this is wrong",
    "you're wrong",
    "youre wrong",
    "wontfix",
    "won't fix",
    "wont fix",
    "by design",
    "working as intended",
    "as intended",
    "intentional",
    "disagree",
    "retract",
    "no change needed",
    "not applicable",
    "invalid finding",
    "misread",
    "dismiss",
)


def is_rejection(text: str) -> bool:
    """Whether a reply rejects the finding it answers.

    "Good catch, fixing" is not a dismissal — that finding should keep being
    reported until the code actually changes, at which point it stops
    reproducing on its own.
    """
    lowered = (text or "").lower()
    return any(p in lowered for p in REJECTION_PHRASES)


def finding_severity_from_body(body: str) -> str | None:
    """The severity rendered in an inline finding thread, if readable."""
    match = re.search(r"\*\*(P[0-2])\*\*", body or "")
    return match.group(1) if match else None


def _thread_entry(body: str) -> dict | None:
    """The stored fingerprint entry on a comment body, if readable.

    `None` when the comment is not ours, predates the fingerprint marker, or
    its JSON is corrupt — all of which mean "cannot be matched against a
    finding", which is the only distinction any caller needs.
    """
    m = _inline_fp_re().search(body or "")
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("fp") else None


def fetch_review_comments(repo: str, number: int) -> list[dict]:
    """Every inline review comment on the PR — roots and replies alike.

    One fetch serves both reply-derived signals below, so neither pays for its
    own round-trip. GraphQL cannot replace it: `in_reply_to_id` is the REST
    field that says which comment answers which, and resolution state is the
    GraphQL field REST does not expose. Each signal needs the other's endpoint.
    """
    comments: list[dict] = _io.gh_paginated_list(
        ["api", f"repos/{repo}/pulls/{number}/comments"]
    )
    return comments


def _is_machine_comment(body: str) -> bool:
    """Whether a comment was written by this reviewer rather than a person.

    Keyed on our marker at the START of the body, which is where the reviewer
    stamps it, and NOT on the author login: `TimeToBuildBob` is shared by the
    reviewer, Bob's own human-directed comments, and every agent of ours, so a
    login test cannot separate them (the same collision that makes
    `resolvedBy.login` useless — see `resolution_dismissals`). A quote-reply
    that includes our marker inside a blockquote begins with ">", so this does
    not misread a human quoting the finding as the machine writing it.
    """
    first = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    return first.lstrip().startswith(_io.MARKER_PREFIX)


def _classify_review_comments(
    comments: Sequence[Mapping[str, object]],
) -> tuple[dict[int, dict], dict[int, list[str]]]:
    """Split review comments into our finding roots and the replies to them.

    Returns `({root comment id: stored entry}, {root comment id: reply bodies})`.

    `in_reply_to_id` is set on replies and only on replies, so it — not the body
    — is what separates an answer from the finding it answers. Two decisions
    ride on that: a quote-reply ("> <our finding>\\n\\nNot a bug") must not be
    misread as another finding of ours, and a second root-level finding must not
    be miscounted as a reply to the first.
    """
    ours: dict[int, dict] = {}
    replies: dict[int, list[str]] = {}
    for c in comments:
        body = str(c.get("body") or "")
        parent = c.get("in_reply_to_id")
        if isinstance(parent, int):
            if not _is_machine_comment(body):
                replies.setdefault(parent, []).append(body)
            continue
        entry = _thread_entry(body)
        cid = c.get("id")
        if entry is not None and isinstance(cid, int):
            ours[cid] = entry
    return ours, replies


def rejected_findings(comments: Sequence[Mapping[str, object]]) -> list[dict]:
    """Suppression entries for findings whose threads carry a *rejecting* reply.

    The weaker of the two dismissal signals — it is substring matching on prose
    — and the one that covers a human who argues back without touching the
    resolve button. Unchanged by the resolution signal: a rejecting reply
    dismisses on its own, resolved or not. See `filter_suppressed` for why
    "raised once, never again" would hide real unfixed bugs behind a 5/5.
    """
    ours, replies = _classify_review_comments(comments)
    posted_at = _comment_posting_heads(comments)
    out: list[dict] = []
    for cid, entry in ours.items():
        if not any(is_rejection(b) for b in replies.get(cid, ())):
            continue
        dismissed = {**entry, "why": "dismissed"}
        # The entry's line anchor is in the coordinates of the commit the
        # finding was posted against; stamping it is what lets the diff guard
        # in `filter_suppressed` tell when those exact lines were rewritten.
        head = posted_at.get(cid)
        if head and "head" not in dismissed:
            dismissed["head"] = head
        out.append(dismissed)
    return out


def _comment_posting_heads(comments: Sequence[Mapping[str, object]]) -> dict[int, str]:
    """``{comment id: original_commit_id}`` — the commit a comment was posted on.

    ``original_commit_id`` (not ``commit_id``) because GitHub moves
    ``commit_id`` forward as the PR is pushed, while the line anchor stored in
    our fingerprint block never moves: it is a line at the original commit.
    """
    heads: dict[int, str] = {}
    for c in comments:
        cid, sha = c.get("id"), c.get("original_commit_id")
        if isinstance(cid, int) and isinstance(sha, str) and sha:
            heads[cid] = sha
    return heads


def incremental_diff(
    repo: str,
    base: str,
    head: str,
    *,
    run_gh: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    """The diff from ``base`` to ``head`` when ``base`` is an ancestor of ``head``.

    ``None`` when it cannot be decided: the compare failed, ``base`` was
    force-pushed away, or the history diverged (a rebase). A three-dot compare
    on diverged history diffs from the merge base, whose coordinates are not
    ``base``'s, so using it would place a dismissal on the wrong lines.
    """

    def _gh(args: list[str]) -> str | None:
        if run_gh is not None:
            return run_gh(args)
        try:
            proc = subprocess.run(
                ["gh", *args], capture_output=True, text=True, check=False, timeout=60
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    endpoint = f"repos/{repo}/compare/{base}...{head}"
    status = _gh(["api", endpoint, "--jq", ".status"])
    if status is None or status.strip() not in ("ahead", "identical"):
        return None
    if status.strip() == "identical":
        return ""
    return _gh(["api", "-H", "Accept: application/vnd.github.diff", endpoint])


def guarded_filter_suppressed(
    findings: Sequence[dict],
    suppressed: Sequence[Mapping[str, object]],
    *,
    repo: str,
    head_sha: str,
    diff_for: Callable[[str], str | None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """`filter_suppressed`, plus the diff guard for entries stamped with a head.

    The PR diff must never be the guard's input: it always contains the lines a
    finding was written against, so it would re-enable every suppression. Each
    muted finding is instead re-checked against the incremental diff from the
    commit its dismissal was anchored to (``entry["head"]``) up to ``head_sha``.
    Unstamped entries, and entries whose diff cannot be decided, keep
    suppressing — the library's own default.

    Diffs are fetched only for heads that actually muted something, once each.
    """
    kept, muted = filter_suppressed(findings, suppressed)
    if not muted:
        return kept, muted
    fetch = diff_for or (lambda base: incremental_diff(repo, base, head_sha))
    diffs: dict[str, str | None] = {}
    revived: set[int] = set()
    for f in muted:
        entry = next((e for e in suppressed if matches_entry(f, e)), None)
        base = str((entry or {}).get("head") or "")
        if (
            not entry
            or not base
            or head_sha.startswith(base)
            or base.startswith(head_sha)
        ):
            continue
        if base not in diffs:
            diffs[base] = fetch(base)
        diff = diffs[base]
        if not diff:
            continue
        back, _ = filter_suppressed([f], [entry], diff, base_head=base)
        if back:
            revived.add(id(f))
    if not revived:
        return kept, muted
    still_muted = {id(f) for f in muted} - revived
    return (
        [f for f in findings if id(f) not in still_muted],
        [f for f in muted if id(f) in still_muted],
    )


def replied_fingerprints(comments: Sequence[Mapping[str, object]]) -> set[str]:
    """Fingerprints of our findings that somebody actually answered.

    Any human reply counts — agreement, a question, "handled in the follow-up".
    Rejection is not required: this measures *engagement*, and it is
    `resolution_dismissals` that pairs it with a resolution.
    """
    ours, replies = _classify_review_comments(comments)
    return {str(entry["fp"]) for cid, entry in ours.items() if replies.get(cid)}


def resolved_finding_entries(
    threads: Sequence[Mapping[str, object]],
) -> list[dict]:
    """Our own inline findings whose threads are resolved, deduped by fingerprint.

    Says nothing about *who* resolved them or *why* — that judgement belongs to
    `resolution_dismissals`. Callers that want the raw set (the marker backfill)
    use this directly.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for t in threads:
        if not t.get("isResolved"):
            continue
        body = str(t.get("body") or "")
        if inline_finding_marker() not in body:
            continue  # a human thread; its resolution says nothing about us
        entry = _thread_entry(body)
        if not entry:
            continue  # unmatchable: cannot be suppressed even if we wanted to
        fp = str(entry["fp"])
        if fp in seen:
            continue
        seen.add(fp)
        out.append(entry)
    return out


def resolution_dismissals(
    threads: Sequence[Mapping[str, object]],
    auto_resolved: Collection[str] = (),
    replied: Collection[str] = (),
) -> list[dict]:
    """Suppression entries for findings that were answered AND then resolved.

    **The predicate, stated once:**

        dismissed  ==  the thread is one of ours (carries the inline finding marker)
                   AND it carries a readable fingerprint
                   AND thread.isResolved
                   AND that fingerprint is NOT in the accumulated `auto_resolved`
                   AND somebody replied to the thread  (`replied`)

    Resolution is the stronger dismissal signal (Erik, 2026-08-10): pressing
    *Resolve conversation* is deliberate and has no wording to pattern-match,
    where `is_rejection` is substring matching on prose and misses any rebuttal
    phrased outside its allow-list. But it takes two conditions to be *safe*:

    **`auto_resolved` — the load-bearing guard.** This reviewer resolves threads
    itself: `reconcile_inline_threads` retires findings that no longer reproduce,
    plus any markered thread it cannot match. Identity cannot separate the two:
    GraphQL reports `resolvedBy.login == TimeToBuildBob` for our reviewer AND for
    every agent of ours that resolves a thread by hand (verified on
    gptme/gptme#3468, where even Greptile-authored threads our agent resolved
    report that login). So "isResolved" alone would mean: finding fixed ->
    auto-resolved -> code regresses -> **permanently suppressed**, a real bug
    hidden behind a 5/5 — exactly what `filter_suppressed` exists to prevent.
    The ledger accumulates across runs in the marker (`accumulate_auto_resolved`),
    so a thread we retired ten runs ago is still recognised as ours today.

    **`replied` — reply AND resolution (Erik, 2026-08-10: "a reply AND a
    resolution sounds wise").** Our agents resolve threads fairly casually, to
    tidy a PR's conversation list rather than to answer anything. Suppression
    means *never raise this again even if it still reproduces*, which is far too
    strong a consequence for a housekeeping click. A reply is the evidence that
    somebody actually read the finding and formed a view: a considered rejection
    leaves words behind, a tidy-up does not. The reply need not *say* anything
    rejecting — that is the separate, older phrase path (`rejected_findings`),
    which still stands on its own.

    **Note (2026-08-11): `filter_suppressed` no longer honors why="resolved"
    entries.** This function still RECORDS resolution dismissals in the suppressed
    ledger (for audit trail and potential future use), but the filter no longer
    acts on them. Resolution is a claim; only explicit rejections (why="dismissed")
    suppress findings now. See `filter_suppressed` in ``ai_review_lib.py``.
    """
    ours_already = set(auto_resolved or ())
    answered = set(replied or ())
    return [
        {**entry, "why": "resolved"}
        for entry in resolved_finding_entries(threads)
        if str(entry["fp"]) not in ours_already and str(entry["fp"]) in answered
    ]


def dismissed_findings(
    repo: str,
    number: int,
    *,
    threads: Sequence[Mapping[str, object]] | None = None,
    auto_resolved: Collection[str] = (),
    comments: Sequence[Mapping[str, object]] | None = None,
) -> list[dict]:
    """Every finding dismissed on this PR, by either signal.

    Dismissal is the ONLY way a finding enters suppression. Emitting one never
    does: see `filter_suppressed` for why "raised once, never again" would hide
    real unfixed bugs behind a 5/5.

    Two signals feed the suppressed ledger (only the first now suppresses in
    ``filter_suppressed`` — see note below):

    * a **rejecting reply** (`rejected_findings`) — words that argue back,
      whether or not the thread was ever resolved; stored as why="dismissed"
      and honored by ``filter_suppressed``.
    * a **reply plus a resolution** by someone other than this reviewer
      (`resolution_dismissals`) — engagement followed by a deliberate close;
      stored as why="resolved" but **no longer suppresses** (see note).

    `threads` is passed in rather than fetched here so the caller's single
    GraphQL read serves both this and reconciliation; the REST comments are
    fetched once here and serve both reply-derived signals.
    """
    if comments is None:
        comments = _io.fetch_review_comments(repo, number)
    entries = rejected_findings(comments)
    known = {e.get("fp") for e in entries}
    # An explicit disposition stamp outranks the `auto_resolved` ledger. The
    # ledger is a *proxy* for "this reviewer resolved it", needed only because
    # `resolvedBy.login` is uninformative; the stamp is direct evidence that an
    # agent read the finding and closed it deliberately. Subtracting it here
    # keeps the legacy backfill (which adopts every pre-existing resolution as
    # ours) from swallowing a disposition made moments earlier.
    effective_auto = set(auto_resolved or ()) - disposed_fingerprints(comments)
    for entry in resolution_dismissals(
        threads or [], effective_auto, replied_fingerprints(comments)
    ):
        if entry["fp"] not in known:
            entries.append(entry)
            known.add(entry["fp"])
    return entries


#: Fingerprints of our own auto-resolutions retained in the marker. Bounded for
#: the same reason as ``MAX_SUPPRESSED`` — the marker rides inside a GitHub
#: comment — but deliberately far larger, because eviction here is not
#: symmetric with eviction there: a forgotten auto-resolution leaves a resolved
#: thread on the PR that the next run reads as somebody else's dismissal. At 12
#: hex chars plus JSON quoting that is ~3 KB, against a 65 KB comment limit, and
#: a PR would need 200 DISTINCT retired findings to reach it (entries are
#: deduped, so re-reviews of the same findings do not consume budget).
MAX_AUTO_RESOLVED = 200


def carried_dispositions(state: Mapping[str, object]) -> dict[str, dict]:
    """The prior marker's ``dispositions`` map, safe to carry into a re-publish.

    Carried forward exactly like ``auto_resolved`` and ``suppressed``, and read
    the same way: an absent map means "nothing has been disposed".

    A re-publish at the same head (consensus upgrade, ``--force``) builds a
    *fresh* marker via ``build_marker_state``, which has no ``dispositions``
    key. Dropping the map silently erased every reconciliation written by
    ``rerender_after_disposition``, so the event feed's ``ai_review_verdict``
    read the finding as outstanding again and PM re-emitted
    ``reviewer_needs_fix`` every cooldown hour. Observed on 13 Bob PRs on
    2026-10-14, each carrying a thread disposition the marker never recorded.
    """
    prior = state.get("dispositions")
    if not isinstance(prior, Mapping):
        return {}
    return {
        str(fp): dict(payload)
        for fp, payload in prior.items()
        if isinstance(fp, str) and isinstance(payload, Mapping)
    }


def accumulate_auto_resolved(
    previous: Sequence[object], added: Iterable[str]
) -> list[str]:
    """Merge newly auto-resolved fingerprints into the stored list.

    Additive and deduped, oldest first, exactly like ``suppressed`` — a
    fingerprint we retired two runs ago must still be recognised as ours, or the
    hazard in `resolution_dismissals` reopens the moment the entry falls out.
    """
    out = [p for p in previous if isinstance(p, str) and p]
    seen = set(out)
    for fp in added:
        if fp and fp not in seen:
            out.append(fp)
            seen.add(fp)
    return out[-MAX_AUTO_RESOLVED:]


# ---------------------------------------------------------------------------
# Review threads: keep what still holds, resolve what no longer does
#
# Re-review used to DELETE every inline comment carrying INLINE_FINDING_MARKER
# and re-post the current set. That destroyed review history on every pass —
# and worse, it orphaned human replies: a reply is not markered so it survived,
# but the finding it answered vanished, leaving an argument with one side
# missing. It also meant GitHub's own `resolved` state was never set, so no
# consumer downstream could read "this finding is settled" at all.
#
# Resolving is what that state is for (Erik, 2026-08-10). A finding that still
# reproduces keeps its existing thread untouched — the thread, its replies, and
# its age are the record of the argument. A finding that no longer reproduces
# gets its thread RESOLVED: collapsed, still readable, still linked.
#
# Nothing here ever deletes, and nothing here ever touches a thread that does
# not carry our marker.
# ---------------------------------------------------------------------------

#: One page of review threads with the state needed to reconcile them: the node
#: id (required by `resolveReviewThread`), whether it is already resolved or
#: outdated, and the ROOT comment body — the root is the only comment that
#: carries our fingerprint, so replies can never be mistaken for findings.
_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            totalCount
            nodes { databaseId author { login } body }
          }
        }
      }
    }
  }
}
"""

#: Supplement query: fetches the tail of the thread list with `last:` ordering.
#: Used when totalCount > fetched nodes due to GitHub eventual consistency.
_REVIEW_THREADS_TAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $last: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(last: $last) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            totalCount
            nodes { databaseId author { login } body }
          }
        }
      }
    }
  }
}
"""

#: Pagination guard. 100 threads a page; a PR that genuinely has 2000 review
#: threads is not one this reviewer can help with anyway.
MAX_THREAD_PAGES = 20

#: Cap on threads to fetch in the eventual-consistency tail supplement.
_MAX_SUPPLEMENT_THREADS = 20


def _thread_node_to_dict(node: dict) -> dict:
    root = ((node.get("comments") or {}).get("nodes") or [{}])[0] or {}
    total_count = (node.get("comments") or {}).get("totalCount")
    return {
        "id": str(node["id"]),
        "root_comment_id": root.get("databaseId"),
        "isResolved": bool(node.get("isResolved")),
        "isOutdated": bool(node.get("isOutdated")),
        "author": str((root.get("author") or {}).get("login") or ""),
        "replyCount": (max(total_count - 1, 0) if isinstance(total_count, int) else 0),
        "body": str(root.get("body") or ""),
    }


def fetch_review_threads(
    repo: str,
    number: int,
    *,
    _out_declared_total: list[int | None] | None = None,
    _max_retries: int = 3,
    _retry_delay: float = 5.0,
) -> list[dict]:
    """Every review thread: `{id, isResolved, isOutdated, body}` for the root.

    Read-only. The REST review-comments endpoint cannot answer this — resolved
    state and the thread node id exist only in GraphQL, and the node id is what
    `resolveReviewThread` takes.

    GitHub's GraphQL has eventual consistency for newly-published inline threads:
    `totalCount` is authoritative, but `first:N` pagination can miss a just-created
    thread. When that gap is detected (totalCount > fetched nodes), a supplemental
    `last:` fetch fills in the missing threads so dispose can find them immediately
    after publish (gptme-contrib#1674, observed 2026-09-17).

    If the supplement still cannot recover the declared count, the whole fetch is
    retried with a short backoff to ride out GitHub's propagation window — a
    freshly-published thread is invisible to BOTH `first:` and `last:` queries
    until the lag clears, so the only reliable recovery is to re-query after a
    delay. `_max_retries`/`_retry_delay` are injectable for tests.

    Pass `_out_declared_total=container` (a single-element list) to receive the
    GitHub-declared thread count after the call.  Useful for callers that need to
    distinguish "thread genuinely absent" from "GitHub eventual-consistency lag".
    """
    owner, _, name = repo.partition("/")

    def _fetch_once() -> tuple[list[dict], int | None]:
        threads: list[dict] = []
        cursor: str | None = None
        declared_total: int | None = None
        for _ in range(MAX_THREAD_PAGES):
            cmd = [
                "api",
                "graphql",
                "-f",
                f"query={_REVIEW_THREADS_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={number}",
            ]
            if cursor:
                cmd += ["-F", f"cursor={cursor}"]
            data = _io.gh_json(cmd)
            if not isinstance(data, dict):
                break
            pr = (
                ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
            ) or {}
            block = pr.get("reviewThreads") or {}
            if declared_total is None:
                raw_count = block.get("totalCount")
                if isinstance(raw_count, int):
                    declared_total = raw_count
            for node in block.get("nodes") or []:
                if not isinstance(node, dict) or not node.get("id"):
                    continue
                threads.append(_thread_node_to_dict(node))
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                break

        # Supplement: GitHub's eventual consistency can cause `first:100` to return
        # fewer nodes than totalCount for a just-published thread. Fetch from the
        # tail with `last:` to capture the missing ones, deduplicated by id.
        if isinstance(declared_total, int) and declared_total > len(threads):
            missing = min(declared_total - len(threads), _MAX_SUPPLEMENT_THREADS)
            tail_cmd = [
                "api",
                "graphql",
                "-f",
                f"query={_REVIEW_THREADS_TAIL_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={number}",
                "-F",
                f"last={missing}",
            ]
            tail_data = _io.gh_json(tail_cmd)
            if isinstance(tail_data, dict):
                tail_pr = (
                    ((tail_data.get("data") or {}).get("repository") or {}).get(
                        "pullRequest"
                    )
                ) or {}
                tail_block = tail_pr.get("reviewThreads") or {}
                seen_ids = {t["id"] for t in threads}
                for node in tail_block.get("nodes") or []:
                    if not isinstance(node, dict) or not node.get("id"):
                        continue
                    if str(node["id"]) in seen_ids:
                        continue
                    threads.append(_thread_node_to_dict(node))
        return threads, declared_total

    threads: list[dict] = []
    declared_total: int | None = None
    for attempt in range(_max_retries):
        threads, declared_total = _fetch_once()
        if declared_total is None or declared_total <= len(threads):
            break
        if attempt < _max_retries - 1:
            print(
                f"WARNING: fetch_review_threads({repo}#{number}): fetched "
                f"{len(threads)}/{declared_total} threads — GitHub eventual-consistency "
                f"lag (both first: and last: queries missed a recently-published "
                f"thread). Retrying in {_retry_delay:.0f}s (attempt {attempt + 1}/"
                f"{_max_retries}).",
                file=sys.stderr,
            )
            time.sleep(_retry_delay)

    # Warn if the retries still didn't recover all threads — both first: and
    # last: queries can miss a freshly-published thread during GitHub's
    # eventual-consistency window (observed on gptme-contrib#1674, 2026-09-17).
    # Callers (e.g. ai-review-dispose) should treat this as a transient lag and
    # retry later rather than concluding the thread is absent.
    if isinstance(declared_total, int) and declared_total > len(threads):
        print(
            f"WARNING: fetch_review_threads({repo}#{number}): fetched "
            f"{len(threads)}/{declared_total} threads after {_max_retries} attempt(s) "
            f"— GitHub eventual-consistency lag persists. Callers should treat this "
            f"as a transient lag and retry later.",
            file=sys.stderr,
        )

    if _out_declared_total is not None:
        _out_declared_total.append(declared_total)

    return threads


def open_finding_threads(threads: Sequence[Mapping[str, object]]) -> list[dict]:
    """Our own UNRESOLVED inline-finding threads, with their stored entry.

    `entry` is `None` when the thread is ours but predates the fingerprint
    comment (or its JSON is corrupt) — recognisable as machine-written, but not
    matchable against a fresh finding.
    """
    out: list[dict] = []
    for t in threads:
        if t.get("isResolved"):
            continue
        body = str(t.get("body") or "")
        if inline_finding_marker() not in body:
            continue  # a human thread; never ours to touch
        out.append(
            {
                "id": str(t.get("id") or ""),
                "entry": _thread_entry(body),
                "severity": finding_severity_from_body(body),
            }
        )
    return [t for t in out if t["id"]]


def reconcile_inline_threads(
    findings: Sequence[Mapping[str, object]],
    threads: Sequence[Mapping[str, object]],
    *,
    held_fps: Collection[str] = (),
) -> tuple[dict[int, dict[str, str]], list[str]]:
    """Match current findings against live threads.

    Returns `({index of finding -> thread identity}, thread ids that no longer
    correspond to any finding)`. A thread identity contains both its
    fingerprint and the severity rendered on the thread.

    A finding in the first mapping must NOT be re-posted — its thread is
    already on the diff, and posting again is how you get four comments for
    three findings. A thread in the second list is resolved, never deleted.

    The mapping carries the **thread's** fingerprint, not the fresh finding's,
    and that distinction is the whole point. `matches_entry` deliberately falls
    back to a token match, so a reworded finding matches a thread whose
    fingerprint differs from its own. Publishing the fresh fingerprint in the
    marker while keeping the old thread hands the self-merge gate an identity
    that no thread on the PR carries — undisposable by construction, and the
    PR blocks on `current marker findings lack disposed threads` until an
    unnecessary review round rewrites it (observed on ErikBjare/bob#1170).
    Callers must publish what is on the diff, which is what this returns. The
    severity has the same constraint: when a later pass regrades a kept P2
    thread as P1, publishing P1 in the marker names a blocking (fp, severity)
    pair that no thread carries and therefore cannot be disposed. The marker
    must retain the kept thread's rendered severity; the historical body stays
    immutable and a genuinely new defect still gets a new thread.

    Threads carrying our marker but no readable entry land in the stale list:
    we cannot prove they still hold, and resolving preserves them verbatim
    while the current pass re-posts whatever is actually live. `_thread_entry`
    only yields an entry with a truthy `fp`, so every kept index has one.
    """
    live = open_finding_threads(threads)
    kept: dict[int, dict[str, str]] = {}
    claimed: set[str] = set()
    for i, f in enumerate(findings):
        for t in live:
            if t["id"] in claimed or not t["entry"]:
                continue
            if matches_entry(f, t["entry"]):
                claimed.add(t["id"])
                kept[i] = {
                    "fp": str(t["entry"]["fp"]),
                    "severity": str(t.get("severity") or "")
                    or normalize_severity(f.get("severity")),
                }
                break
    return kept, [
        t["id"]
        for t in live
        if t["id"] not in claimed and not (t["entry"] and t["entry"]["fp"] in held_fps)
    ]


def retain_reopened_holds(
    findings: list[dict],
    threads: Sequence[Mapping[str, object]],
    auto_resolved: Collection[str],
    comments: Sequence[Mapping[str, object]],
) -> dict[int, dict[str, str]]:
    """Carry deliberately reopened threads, even when a clean pass omits them.

    An unresolved thread whose fingerprint we previously retired is evidence
    somebody reopened it. Keep its published identity and severity until it is
    resolved again; do not turn a model's silence into policy adjudication.
    This runs after model filtering and independently of inline posting.
    """
    held: dict[int, dict[str, str]] = {}
    for thread in threads:
        body = str(thread.get("body") or "")
        entry = _thread_entry(body)
        if (
            thread.get("isResolved")
            or inline_finding_marker() not in body
            or not entry
            or entry["fp"] not in auto_resolved
        ):
            continue
        # Prefer the existing matching finding, but retain the thread when the
        # model no longer reproduces it. The marker must still name that thread.
        index = next(
            (
                i
                for i, finding in enumerate(findings)
                if i not in held and matches_entry(finding, entry)
            ),
            None,
        )
        if index is None:
            index = len(findings)
            findings.append(
                {
                    "file": entry.get("file") or "",
                    "line": entry.get("line"),
                    "body": "Reopened review hold; awaiting explicit adjudication.",
                }
            )
        finding = findings[index]
        severity = finding_severity_from_body(body) or "P1"
        finding["severity"] = severity
        finding.pop("non_blocking", None)
        # Quote only replies attached to THIS root; never borrow another
        # thread's explanation. A bare GraphQL reopen still establishes a hold.
        root_id = thread.get("root_comment_id")
        replies = [
            str(comment.get("body") or "")
            for comment in comments
            if type(root_id) is int and comment.get("in_reply_to_id") == root_id
        ]
        detail = replies[-1] if replies else "No reopening reply recorded."
        finding["verification"] = f"Reopened review hold: {detail}"
        held[index] = {"fp": str(entry["fp"]), "severity": severity}
    return held


def apply_kept_identities(
    findings: list[dict],
    kept: Mapping[int, Mapping[str, str] | str],
) -> None:
    """Rewrite kept findings to the severity their live thread publishes.

    ``reconcile_inline_threads`` keeps the thread's rendered severity when a
    later pass regrades the same finding, because the marker must name an
    identity that exists on the PR. The score, the rendered verdict and the
    marker are all derived from ``findings`` afterwards, so they have to see
    that same severity — otherwise the score claims a P1 that ``findings``
    never publishes, and the self-merge gate refuses the contradictory marker.

    Mutated in place: ``render`` identifies inline-posted findings by object
    id (``anchored``), so replacing the dicts would silently reclassify them.
    """
    for index, finding in enumerate(findings):
        identity = kept.get(index)
        if isinstance(identity, Mapping) and identity.get("severity"):
            finding["severity"] = str(identity["severity"])


_RESOLVE_MUTATION = (
    "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
    "{ thread { isResolved } } }"
)


def resolve_review_thread(thread_id: str, repo: str | None = None) -> bool:
    """Mark one review thread resolved. Never deletes anything.

    ``repo`` scopes the attribution wrapper's cache invalidation to the mutated
    repository. ``resolveReviewThread`` is addressed by node ID, so the wrapper
    finds no repo in argv and otherwise flushes EVERY repo's cached reads —
    measured 2026-09-26 at ~23 full flushes/hour, which is why read hits are
    near zero fleet-wide.
    """
    env = {**os.environ, "BOB_GRAPHQL_MUTATION_REPO": repo} if repo else None
    proc: subprocess.CompletedProcess = _io.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_RESOLVE_MUTATION}",
            "-F",
            f"id={thread_id}",
        ],
        env=env,
    )
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Re-rendering the summary after an inline disposition (gptme/gptme#3706)
#
# The summary comment's verdict line is written once, at review time, and
# never touched again — `dispose()` replies on a THREAD, a different GitHub
# API, and never revisits it. Traced live on gptme/gptme#3706: the summary
# said "Confidence Score: 3/5 — One P1 finding" for four days after the P1's
# thread was resolved (via a rejection that itself cited a sha not on the PR
# head — see `ai-review-dispose.py`'s sha verification). Erik read the
# summary — the one place a human is meant to look — and it was stale. Two
# sources of truth is the bug; this section makes the summary the up-to-date
# one, computed from the same thread state the self-merge gate reads, so it
# can never disagree with the gate about what still blocks.
#
# Deliberately does NOT rewrite the original verdict/finding sections the
# review pass wrote — those are the historical record of what the reviewer
# found and said at that head, and overwriting them in place is exactly the
# kind of silent rewrite this system goes out of its way to avoid elsewhere
# (see the append-only journal discipline, or `history` on the marker). The
# disposition state is spliced in as its own delimited block, inserted right
# under the top heading so it is the first thing a reader sees — same idiom
# `render_unreviewable_body` already uses for a different kind of correction.
# ---------------------------------------------------------------------------


def disposition_note_markers() -> tuple[str, str]:
    ns = _io.MARKER_NAMESPACE
    return f"<!-- {ns}-dispositions -->", f"<!-- /{ns}-dispositions -->"


def _disposition_note_re() -> re.Pattern[str]:
    start, end = disposition_note_markers()
    return re.compile(re.escape(start) + ".*?" + re.escape(end), re.DOTALL)


def inline_finding_marker() -> str:
    return f"<!-- {_io.MARKER_NAMESPACE}-finding -->"


def disposition_note(verdict: str, table: str) -> str:
    """The visible block spliced into the summary after a disposition."""
    start, end = disposition_note_markers()
    lines = [
        start,
        f"### {verdict}",
        "",
        "<sub>Updated after inline dispositions on finding threads — this is "
        "the current state; the verdict below is frozen at review time and is "
        "kept as the historical record of that pass.</sub>",
        "",
    ]
    if table:
        lines += [
            "<details><summary>Finding disposition</summary>",
            "",
            table,
            "",
            "</details>",
            "",
        ]
    lines.append(end)
    return "\n".join(lines)


def render_disposed_body(existing_body: str, note: str) -> str:
    """Splice *note* into *existing_body*, idempotently.

    A second re-render (a follow-up disposition) replaces the previous note in
    place rather than stacking a second one — matching the
    `render_unreviewable_body` idiom this borrows. First splice inserts right
    after the top heading, so the current state reads before the frozen one.
    """
    body = existing_body or ""
    if _disposition_note_re().search(body):
        return _disposition_note_re().sub(lambda _: note, body, count=1)
    heading = "## 🤖 AI code review"
    idx = body.find(heading)
    if idx == -1:
        return f"{note}\n\n{body}"
    insert_at = idx + len(heading)
    return f"{body[:insert_at]}\n\n{note}\n{body[insert_at:]}"


def build_disposition_rows(
    threads: Sequence[Mapping[str, object]],
    dispositions: Mapping[str, Mapping[str, object]],
    current_findings: Sequence[Mapping[str, object]] | None = None,
    *,
    auto_resolved: Collection[str] = (),
    expected_author: str | None = None,
) -> list[dict]:
    """One row per our own finding thread, current head or not.

    Includes RESOLVED threads (dispositions live only on those) as well as
    open ones — unlike `open_finding_threads`, which exists specifically to
    exclude resolved threads for re-posting. ``blocking`` and ``shortfall`` are
    both produced by the shared gate predicate. An outdated, resolved finding
    absent from the latest marker is superseded, not silently disposed by a
    human.

    ``current_findings=None`` preserves the legacy fail-closed behaviour for
    marker comments that predate per-fingerprint findings. A present empty list
    is authoritative, just like the self-merge gate's marker contract.
    ``auto_resolved`` is the reviewer's durable record that it retired a thread
    after a later pass stopped reproducing the finding; this is stronger than
    GitHub's diff-position ``isOutdated`` flag, which can stay false across a
    rebase when an old line still maps onto the new diff.
    """
    current_fps = (
        {
            str(f.get("fp"))
            for f in current_findings
            if isinstance(f, Mapping) and f.get("fp")
        }
        if current_findings is not None
        else None
    )
    rows: list[dict] = []
    for t in threads:
        body = str(t.get("body") or "")
        if inline_finding_marker() not in body:
            continue  # a human or Greptile thread — never ours to summarize
        if expected_author and t.get("author") != expected_author:
            continue  # marker-shaped, but not written by our reviewer
        entry = _thread_entry(body)
        if not entry or not entry.get("fp"):
            continue
        fp = str(entry["fp"])
        severity = finding_severity_from_body(body)
        disp = dispositions.get(fp)
        superseded = bool(
            current_fps is not None
            and fp not in current_fps
            and t.get("isResolved")
            and (t.get("isOutdated") or fp in auto_resolved)
        )
        row = {
            "fp": fp,
            "file": entry.get("file") or "",
            "line": entry.get("line"),
            "severity": severity,
            "isResolved": bool(t.get("isResolved")),
            "isOutdated": bool(t.get("isOutdated")),
            "replied": bool(t.get("replyCount")),
            "superseded": superseded,
            "disposition": disp,
        }
        shortfalls = blocking_shortfall([row])
        row["blocking"] = bool(shortfalls)
        row["shortfall"] = shortfalls[0]["reason"] if shortfalls else None
        rows.append(row)

    # A current marker finding with no inline thread (the reviewer could not
    # anchor it to a diff line, or the thread was never posted) is still a
    # finding the self-merge gate counts. Without a row it was invisible here,
    # so the projection said "Safe to merge — no P0/P1 findings" over a marker
    # holding two undisposed P1s (gptme/gptme-cloud#1153, head d091e41b6ed6).
    seen = {r["fp"] for r in rows}
    for f in current_findings or ():
        if not isinstance(f, Mapping) or not f.get("fp"):
            continue
        fp = str(f["fp"])
        if fp in seen:
            continue
        seen.add(fp)
        line = f.get("line")
        row = {
            "fp": fp,
            "file": str(f.get("file") or ""),
            "line": line if isinstance(line, int) else None,
            "severity": normalize_severity(f.get("severity")),
            "isResolved": False,
            "isOutdated": False,
            "replied": False,
            "superseded": False,
            "disposition": dispositions.get(fp),
            "unanchored": True,
        }
        shortfalls = blocking_shortfall([row])
        row["blocking"] = bool(shortfalls)
        row["shortfall"] = (
            f"{row['severity']} finding has no inline thread (not disposed)"
            if shortfalls
            else None
        )
        rows.append(row)
    return rows


# --- writer-port slice 2c: diff shaping, notes and render ---------------

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
SEVERITY_ICON = {"P0": "🛑", "P1": "❌", "P2": "⚠️"}
SEVERITY_LABEL = {"P0": "critical", "P1": "high", "P2": "medium"}

#: Rows in the per-file change table before it is truncated. Greptile's own
#: tables run to a handful of files; a 200-file refactor does not become more
#: glanceable by listing all 200, and the overflow is named rather than hidden.
MAX_FILE_ROWS = 40
#: Undescribed paths listed by name before collapsing to a count.
MAX_UNDESCRIBED_NAMED = 12

# Match whole delimiter runs; shorter/longer runs inside a span are literal.
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)


def md_text(text: str, *, escape_pipes: bool = False) -> str:
    """Make model prose safe to drop into a comment without losing characters.

    The angle brackets are the load-bearing part. GitHub renders raw HTML
    inside Markdown, so a description that faithfully mentions `<think>` would
    render as *nothing* — silently deleting the exact characters this reviewer
    has a documented history of dropping (gptme/gptme#3486). Escaping them
    means an accurate description stays accurate on screen, and an inaccurate
    one stays visibly inaccurate instead of being papered over.

    Escaping happens only OUTSIDE code spans: inside a span Markdown already
    renders `<` literally, and `&lt;` there would show as the entity text.
    Pipes are escaped everywhere (including in code spans, which GFM allows)
    because a bare `|` splits a table cell.
    """
    chunks: list[tuple[bool, str]] = []
    pos = 0
    for m in _CODE_SPAN_RE.finditer(text):
        chunks.append((False, text[pos : m.start()]))
        chunks.append((True, m.group(0)))
        pos = m.end()
    chunks.append((False, text[pos:]))

    out: list[str] = []
    for is_code, chunk in chunks:
        if not is_code:
            chunk = chunk.replace("<", "&lt;").replace(">", "&gt;")
        if escape_pipes:
            chunk = chunk.replace("|", "\\|")
        out.append(chunk)
    return "".join(out)


def _md_code(text: str) -> str:
    """Quote literal labels with a delimiter longer than any embedded run."""
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


#: Public, linkable explanation of what this comment is and how to drive it.
#: ErikBjare/bob#1122 was the old link; that repo is private, so it told an
#: outside maintainer nothing.
REVIEWER_DOC_URL = (
    "https://github.com/gptme/gptme-contrib/blob/master/docs/ai-reviewer.md"
)


def _summary_files(change_summary: dict | None) -> tuple[str, list[dict]]:
    cs = change_summary or {}
    return (
        str(cs.get("summary") or "").strip(),
        [f for f in (cs.get("files") or []) if isinstance(f, dict)],
    )


def render_change_prose(change_summary: dict | None) -> list[str]:
    """The lede: what this PR does, before anything about what is wrong with it.

    Greptile leads with the description and that ordering is right — a reader
    triaging a queue needs "what is this change" before "what is the score",
    and on a zero-finding review the description is the entire message.

    The per-file breakdown does NOT live here; it is reference material and
    sits below the findings (`render_files_changed`).
    """
    summary, files = _summary_files(change_summary)
    if not summary:
        return []
    # The caveat rides with the prose when there is prose; otherwise
    # render_files_changed carries it, so it is never dropped.
    return [md_text(summary), ""]


def render_files_changed(
    change_summary: dict | None, undescribed: list[str] | None = None
) -> list[str]:
    """Per-file breakdown, collapsed: reference material, not the headline.

    Rendered as a **list, not a table** — a deliberate divergence from Greptile,
    which uses a two-column table on all 200 corpus PRs. Column 1 holds an
    unbreakable long path and column 2 holds prose that wraps to several lines,
    so on a phone the table either squeezes the description into a sliver or
    forces horizontal scroll (Erik, 2026-08-08). A list reflows. Do not
    "restore parity" by reverting this; readability beat benchmark fidelity on
    purpose.

    Collapsed rather than cut: it is how a reader checks what the reviewer
    actually read, which is worth keeping and not worth the vertical space in
    the default view (Erik, 2026-08-10).

    Deliberately no mermaid diagram: the benchmark over 200 Greptile reviews
    put diagrams (on ~60% of PRs) at the highest token cost per unit of
    decision value in the whole output.
    """
    summary, files = _summary_files(change_summary)
    if not files:
        return []

    lines = [
        f"<details><summary><b>Files changed ({len(files)})</b> — "
        "the diff as I read it</summary>",
        "",
    ]
    for row in files[:MAX_FILE_ROWS]:
        path = str(row.get("path") or "").strip()
        desc = " ".join(str(row.get("description") or "").split())
        # No pipe escaping: nothing here is a table cell any more, and a
        # `\|` would render as a literal backslash in flowing prose.
        lines.append(f"- {_md_code(path)} — {md_text(desc) or '_(no description)_'}")
    if len(files) > MAX_FILE_ROWS:
        lines.append(f"- _{len(files) - MAX_FILE_ROWS} more changed file(s) not shown_")
    lines.append("")

    # Naming the gap matters: silence here would read as "the reviewer covered
    # everything", which is the assurance this section exists to avoid faking.
    if undescribed:
        named = ", ".join(_md_code(p) for p in undescribed[:MAX_UNDESCRIBED_NAMED])
        extra = (
            f" (+{len(undescribed) - MAX_UNDESCRIBED_NAMED} more)"
            if len(undescribed) > MAX_UNDESCRIBED_NAMED
            else ""
        )
        lines += [f"<sub>Also changed, not described above: {named}{extra}</sub>", ""]

    lines += ["</details>", ""]
    return lines


def is_submodule_only_diff(diff: str) -> bool:
    """True when the diff changes nothing but submodule pointers.

    Such a diff carries no source for the reviewer to reason about — only
    `-Subproject commit <old>` / `+Subproject commit <new>` — so a clean verdict
    on it would assert more than was checked.
    """
    sections = split_file_sections(diff)
    if not sections:
        return False
    for section in sections:
        lines = section.text.splitlines()
        header = lines[
            : next((i for i, ln in enumerate(lines) if ln.startswith("@@")), len(lines))
        ]
        modes = []
        for ln in header:
            if ln.startswith(
                ("old mode ", "new mode ", "new file mode ", "deleted file mode ")
            ):
                modes.append(ln.rsplit(" ", 1)[-1])
            elif re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+ [0-7]{6}", ln):
                modes.append(ln.rsplit(" ", 1)[-1])
        changed = [
            ln
            for ln in lines
            if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
        ]
        # Every section needs positive gitlink evidence, including sections
        # with no text hunks (binary changes, pure renames and mode changes).
        if not modes or set(modes) != {"160000"} or not changed:
            return False
        if not all(
            re.fullmatch(r"[+-]Subproject commit [0-9a-f]{40,64}(?:-dirty)?", ln)
            for ln in changed
        ):
            return False
    return True


#: Paths whose diff is machine output rather than something anyone reviews.
#: Matched with :func:`fnmatch.fnmatch` against the FULL path and against the
#: basename, so `uv.lock` catches `subdir/uv.lock` and `*.min.js` catches any
#: directory. Extending this tuple is the intended way to fix the next
#: "unreviewable because of a generated file" report — keep it here.
#:
#: Deliberately not `str.endswith`: that matched `my-uv.lock` and `vendor.min.js`
#: alike but could never express `*.min.css`, and a substring rule on paths is
#: the kind that quietly grows false positives.
GENERATED_FILE_PATTERNS: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "flake.lock",
    "*.min.js",
    "*.min.css",
)


def _is_generated_file(path: str) -> bool:
    """True if the file is machine-generated and should be excluded from review."""
    name = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat)
        for pat in GENERATED_FILE_PATTERNS
    )


def extract_touched_files_and_reviewable_diff(
    diff: str,
) -> tuple[set[str], str, int, list[dict]]:
    """Split a diff into what gets reviewed and what gets excluded.

    Returns:
        - set of reviewable file paths touched by the PR (generated files are
          omitted unless the diff contains nothing else and is kept whole)
        - diff text with generated file changes removed
        - size in bytes of the reviewable diff
        - one record per excluded file: ``path``, ``added``, ``removed``,
          ``bytes`` — the input to :func:`elision_note`, which is what puts the
          exclusion in front of a human. Excluding a file from review without
          saying so makes the review claim coverage it does not have, which is
          worse than the size-gate bug this exclusion exists to fix.

    A diff of *nothing but* generated files is returned UNCHANGED, with no
    exclusions. Excluding everything leaves an empty diff; an empty diff finds
    nothing; zero findings scores 5/5; and 5/5 is exactly what the self-merge
    gate reads as ready-to-merge. The honest outcome for a lockfile-only PR is
    the one it had before: reviewed if it fits, `diff_too_large` if it does not.
    """
    touched: set[str] = set()
    # Share the sharding parser: it decodes Git's C-quoted UTF-8 paths,
    # prefers image markers (pre-image for deletions), and handles sections
    # without hunks, including binary changes and pure renames.
    sections = split_file_sections(diff)
    reviewable_lines: list[str] = []
    excluded: dict[str, list[str]] = {}
    for section in sections:
        lines = section.text.splitlines(keepends=True)
        path = section.path
        if path is None:
            # Keep malformed preambles reviewable rather than guessing a path
            # and risking a fail-open exclusion.
            reviewable_lines.extend(lines)
            continue
        if _is_generated_file(path):
            excluded.setdefault(path, []).extend(lines)
        else:
            touched.add(path)
            reviewable_lines.extend(lines)

    # Nothing reviewable would survive: keep the diff whole. See docstring.
    if excluded and not reviewable_lines:
        return set(excluded), diff, len(diff.encode("utf-8", errors="replace")), []

    reviewable_diff = "".join(reviewable_lines)
    reviewable_bytes = len(reviewable_diff.encode("utf-8", errors="replace"))

    elided = [
        {
            "path": path,
            "bytes": len("".join(lines).encode("utf-8", errors="replace")),
            **_count_changed_lines(lines),
        }
        for path, lines in excluded.items()
    ]
    return touched, reviewable_diff, reviewable_bytes, elided


def _count_changed_lines(lines: list[str]) -> dict[str, int]:
    """Added/removed line counts for one file's diff section."""
    added = removed = 0
    for raw in lines:
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith("+"):
            added += 1
        elif raw.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


def preload_coverage_note(coverage: Mapping[str, int] | None) -> str:
    """One-line disclosure when changed-file preload coverage is incomplete."""
    if not coverage:
        return ""
    total = int(coverage.get("files_in_diff", 0))
    incomplete = int(coverage.get("files_truncated", 0))
    if total <= 0 or incomplete <= 0:
        return ""
    return (
        f"> ⚠️ **{incomplete} of {total} changed files were not fully read.** "
        "Claims that an identifier is never assigned, used, or called are "
        "discarded unless verification names only fully preloaded files. This "
        "coverage describes the bounded context preload; the diff was reviewed."
    )


def elision_note(elided_files: list[dict] | None) -> str:
    """State, in the posted review, which files were excluded. "" when none.

    Non-negotiable disclosure. The reviewer's output feeds a self-merge gate
    that reads zero findings as ready-to-merge, so a review that quietly
    skipped a changed file launders an unexamined change through a green check.
    Naming the file and its size costs one line and makes the gap auditable.

    Rendered next to the score rather than in the footer: the score is the
    artefact that gets acted on.
    """
    if not elided_files:
        return ""
    parts = [
        (
            f"``{rec.get('path', '?')}`` "
            f"(+{rec.get('added', 0)}/-{rec.get('removed', 0)} lines, "
            f"{int(rec.get('bytes', 0)) // 1024}KB)"
        )
        for rec in elided_files
    ]
    n = len(parts)
    return (
        f"> ⚠️ **{n} generated file{'' if n == 1 else 's'} excluded and NOT "
        f"reviewed:** {', '.join(parts)}. Machine-generated output is removed "
        "from the diff before review — from what the model sees and from the "
        f"size gate alike — so nothing below says anything about "
        f"{'it' if n == 1 else 'them'}."
    )


#: Why a finding was dropped by POLICY, and how to say so. Keyed by the
#: `suppressed_reason` stamped on the finding at the drop site.
#:
#: Only policy drops belong here. A finding removed because it was never valid
#: — self-disclaiming, misquoted, consensus-rejected — is not suppressed
#: evidence, and a human's own dismissal (the suppression ledger) is a settled
#: argument, not a filter acting on the reader's behalf.
#:
#: The round cap is deliberately ABSENT: it no longer drops findings — it
#: downgrades them to informational via :func:`apply_round_cap`, so they are
#: reported rather than suppressed (contrib#1434).
SUPPRESSION_REASONS: dict[str, str] = {
    # Phrased as a noun clause, not a sentence with a subject: these strings are
    # interpolated after counts that may be 1 or many, and "they fall in files…"
    # over a single finding is the kind of small wrongness that makes a reader
    # stop trusting the rest of the comment.
    # Covers BOTH shapes `drop_out_of_scope_findings` produces: a finding in a
    # file the PR never touched, and a finding carrying no `file` at all (a
    # PR-level observation, which cannot be shown in scope either). Saying only
    # "in files this PR did not touch" would be false of the second.
    "out_of_scope": (
        "not anchored to a file this PR changed — pre-existing code, or an "
        "observation about the PR as a whole"
    ),
}


def suppression_note(suppressed: list[dict] | None) -> str:
    """State, in one quiet line, which findings policy dropped. "" when none.

    Same mechanism and same reason as :func:`elision_note`, one level up: that
    one says which *files* went unread, this one says which *findings* were
    found and then withheld. A body reading "No findings on this head" while
    a P2 was dropped is a score that lies to the merge gate — and unlike the
    size gate it replaced, it fails OPEN.

    This is a single factual line, not the multi-paragraph essay about the
    reviewer's own filter that used to print here (contrib#1434). The counts
    are also the raw material for filing these as issues later, which is what
    should happen to an out-of-scope finding.
    """
    if not suppressed:
        return ""
    # Only policy drops are disclosed. A finding that was never valid (self-
    # disclaiming, misquoted, consensus-rejected) or a human's own dismissal is
    # not withheld evidence, so it must not read as one here.
    policy = [
        f for f in suppressed if f.get("suppressed_reason") in SUPPRESSION_REASONS
    ]
    if not policy:
        return ""
    n = len(policy)
    return (
        f"> ℹ️ {n} finding{'' if n == 1 else 's'} not anchored to a file "
        "this PR changed — not reported here."
    )


def _consensus_rejected(consensus: dict | None) -> int:
    """Findings the consensus filter dropped on this run."""
    if not consensus:
        return 0
    rejected = consensus.get("rejected")
    return rejected if isinstance(rejected, int) else 0


def drop_out_of_scope_findings(
    findings: list[dict], touched_files: set[str]
) -> tuple[list[dict], list[dict]]:
    """Filter findings to only those in files the PR touched.

    A finding on a pre-existing code path (file not in touched_files) is noted
    but not a blocker. This prevents review feedback on stale code from forcing
    a re-review cycle on an unrelated PR.

    Returns:
        - in_scope findings (files the PR touched)
        - out_of_scope findings (pre-existing code)
    """
    in_scope = []
    out_of_scope = []

    for finding in findings:
        file_path = finding.get("file", "")
        if file_path in touched_files:
            in_scope.append(finding)
        else:
            out_of_scope.append(finding)

    return in_scope, out_of_scope


def head_path_exists(repo: str, ref: str, path: str) -> bool | None:
    """Whether ``path`` exists at ``ref``; ``None`` means the probe failed.

    Only an explicit GitHub 404 is absence. Authentication, rate-limit, and
    transport failures keep the finding visible rather than laundering an
    unverified path into a mechanical drop.
    """
    proc = _io.run(
        [
            "gh",
            "api",
            "--silent",
            "--method",
            "GET",
            f"repos/{repo}/contents/{quote(path, safe='/')}",
            "-f",
            f"ref={ref}",
        ]
    )
    if proc.returncode == 0:
        return True
    if "HTTP 404" in (proc.stderr or ""):
        return False
    return None


def drop_nonexistent_head_findings(
    findings: list[dict],
    *,
    repo: str,
    ref: str,
    known_existing_paths: Collection[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Drop cited paths proven absent from the reviewed head.

    PR-level findings carry an empty path and stay. Results are cached per path
    because several findings can cite the same file.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    statuses: dict[str, bool | None] = {
        str(path): True for path in known_existing_paths
    }
    for finding in findings:
        path = str(finding.get("file") or "")
        if not path:
            kept.append(finding)
            continue
        if path not in statuses:
            statuses[path] = _io.head_path_exists(repo, ref, path)
        (dropped if statuses[path] is False else kept).append(finding)
    return kept, dropped


def apply_round_cap(
    findings: list[dict], review_round_count: int, max_rounds: int = 5
) -> int:
    """Downgrade P2+ findings to informational once the round cap is reached.

    The hard round cap stops endless re-review cycles: after ``max_rounds``
    rounds on one PR, P2+ findings no longer *block* the merge. That is a
    merge-gating fact, and the self-merge gate already owns it
    (``AI_REVIEW_BLOCKING_SEVERITIES = {P0, P1}``, contrib#1405) — so the
    reviewer must NOT suppress the findings, only stop escalating them. A P2
    that no longer blocks is still worth reading.

    Mutates ``findings`` in place: each P2+ finding gets ``non_blocking=True``
    so it is reported as an informational note rather than dropped into the
    suppression ledger (the old behaviour, which made the findings vanish and
    the body read as a false clean — contrib#1416 — or print an essay about
    its own filter — contrib#1434).

    Returns the number of findings downgraded (0 when the cap is not reached
    or there is nothing to downgrade).
    """
    if review_round_count <= max_rounds or not findings:
        return 0
    capped = [f for f in findings if f.get("severity") not in ("P0", "P1")]
    for f in capped:
        f["non_blocking"] = True
        f["non_blocking_reason"] = "round_cap"
    return len(capped)


def non_blocking_note(findings: list[dict], reason: str) -> str:
    """Brief note for the informational findings section.

    Returns an empty string for unknown reasons or empty finding lists so
    callers can safely append it without producing blank lines.
    """
    if not findings or reason != "out_of_scope":
        return ""
    if all(not f.get("file") for f in findings):
        return (
            "_Each item here is an observation about the PR as a whole — "
            "informational only, not scored._"
        )
    return (
        "_These findings are not anchored to a file this PR changed — "
        "informational only, not scored._"
    )


def consensus_note(consensus: dict | None) -> str:
    """One quiet line saying how the run fell short of the consensus asked for.

    Empty string when it did not. Both kinds of shortfall get a clause, and
    they are genuinely different failures: losing whole *passes* lowers the
    agreement threshold that can be applied at all, while losing *aspect jobs*
    leaves the threshold intact but starves it of evidence — the second is the
    one the pass count used to hide completely.
    """
    if not consensus or not consensus_degraded(consensus):
        return ""
    clauses: list[str] = []
    if consensus_lost_passes(consensus):
        applied = consensus["min_agreement"]
        answered = consensus["answered"]
        clauses.append(
            f"{answered} of {consensus['requested']} passes answered, so "
            f"findings were filtered at {applied}-of-{answered} agreement "
            f"rather than {consensus['min_agreement_requested']}-of-"
            f"{consensus['requested']} — less filtered than usual"
        )
    if consensus_lost_jobs(consensus):
        clauses.append(
            f"{consensus['jobs_answered']} of {consensus['jobs_requested']} "
            "fan-out jobs answered, so the findings above were filtered "
            "against less evidence than the threshold assumes"
        )
    return (
        "<sub>ℹ️ Consensus was degraded on this run: " + "; ".join(clauses) + ".</sub>"
    )


#: Footer text per maintainer command. ``review`` is handled by the sweep's
#: trigger on every host; any other command needs a host-side worker.
_MAINTAINER_COMMAND_TEXT = {
    "review": "`@{login} review` (own line) — fresh review",
    "fix": "`@{login} fix` — a worker acts on the findings",
}


def maintainer_commands() -> tuple[str, ...]:
    """The commands this host's footer may advertise.

    Read from ``_io.MAINTAINER_COMMANDS``. A host that does not set it gets
    review only: a footer must never promise a worker the host does not run.
    Hosts explicitly declare the commands they handle; the writer does not
    discover workers. Invalid declarations (including ``None``) raise ValueError.
    """
    try:
        declared = _io.MAINTAINER_COMMANDS
    except AttributeError:
        return ("review",)
    if isinstance(declared, str):
        commands = (declared,)
    else:
        try:
            commands = tuple(declared)
        except TypeError:
            raise ValueError(f"unsupported maintainer commands: {declared!r}") from None
    unknown = [
        c
        for c in commands
        if not isinstance(c, str) or c not in _MAINTAINER_COMMAND_TEXT
    ]
    if unknown or not commands:
        raise ValueError(f"unsupported maintainer commands: {commands!r}")
    return commands


def maintainer_commands_line(login: str) -> str:
    texts = [
        _MAINTAINER_COMMAND_TEXT[c].format(login=login) for c in maintainer_commands()
    ]
    return f"<sub>{' · '.join(texts)}. Once per comment; 👀 = received.</sub>"


def render(
    findings: list[dict],
    *,
    model: str,
    engine: str,
    elapsed: float,
    head_sha: str,
    full_sha: str,
    repo_slug: str,
    anchored: set[int],
    history: list[dict],
    submodule_only: bool = False,
    change_summary: dict | None = None,
    undescribed: list[str] | None = None,
    consensus: dict | None = None,
    elided_files: list[dict] | None = None,
    preload_coverage: Mapping[str, int] | None = None,
    suppressed_findings: list[dict] | None = None,
    informational_findings: list[dict] | None = None,
    advisory_findings: list[dict] | None = None,
    non_coverage: str = "",
    sharding: Mapping[str, object] | None = None,
) -> str:
    findings, classified_advisories = partition_posting_findings(findings)
    advisory_findings = [*classified_advisories, *(advisory_findings or [])]
    ordered = sorted(
        findings, key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "P2")), 9)
    )

    # Section order follows Greptile's, which reads better than what this used
    # to do (Erik, 2026-08-10): description → score with findings → files
    # changed → a small-text footer. No `<hr>` rules anywhere — headings
    # already separate the sections, and the rules only added vertical noise.
    lines = ["## 🤖 AI code review", ""]
    pointer_bump_only = submodule_only and not ordered

    # 1. What the PR does. Above the verdict deliberately: a reader wants "what
    #    is this change" before "what is wrong with it", and a zero-finding
    #    review is precisely when the description carries the whole message.
    #
    #    Suppressed entirely on a pointer bump: all that diff contains is
    #    `-Subproject commit <old>` / `+Subproject commit <new>`, so anything
    #    written about it describes commits that are not in front of us. A
    #    fluent paragraph about a pointer bump is the same false assurance the
    #    "not reviewed" branch below exists to refuse.
    if not pointer_bump_only:
        lines += render_change_prose(change_summary)

    # 2. The verdict, with its label folded into the heading rather than
    #    repeated as a sentence under it. It is the one line a human triaging a
    #    queue — or PM's re-review loop deciding whether to iterate again —
    #    actually needs. `None` is the abstain case and renders as N/A rather
    #    than as a number: see `confidence_score`.
    n_suppressed = len(suppressed_findings or [])
    n_informational = len(informational_findings or [])
    score = confidence_score(
        findings,
        submodule_only=submodule_only,
        suppressed=n_suppressed,
        informational=n_informational,
        non_coverage=non_coverage,
    )
    lines += [
        f"### {merge_verdict(score, findings)}",
        "",
        f"Confidence {score}/5" if score else "Confidence N/A",
        "",
    ]
    # The rationale sentence earns its line only when there is no score to
    # explain itself: "4/5 — P2 findings only" followed by "No P0/P1 on this
    # head, but 1 P2 issue outstanding" and then a findings headline saying it
    # a third time was three lines for one fact (Erik, 2026-08-20).
    if score is None:
        lines += [
            confidence_rationale(
                score,
                findings,
                suppressed=n_suppressed,
                informational=n_informational,
                submodule_only=submodule_only,
                non_coverage=non_coverage,
            ),
            "",
        ]

    # Say when the score above did not get the consensus it asked for. We tell
    # readers consensus is what makes these scores stable; on a degraded run
    # that property does not hold, and without this line a 3/5 filtered by one
    # surviving pass is indistinguishable from a 3/5 that three passes agreed
    # on. Deliberately quiet — one <sub> line, no warning icon shouting at a
    # reader about a run that is still worth reading, just less filtered.
    #
    # Suppressed ONLY on a submodule pointer bump, where nothing was reviewed
    # and "less filtered than usual" describes filtering that never happened.
    #
    # It is emphatically NOT suppressed on an ordinary review that found
    # nothing. That case looks the most reassuring and is the most dangerous:
    # zero findings scores 5/5, 5/5 is what the self-merge gate reads as
    # ready-to-merge, and a degraded run reaches zero findings by losing its
    # evidence rather than by the code being clean. Suppressing the note there
    # is how a starved review launders itself into a merge. An earlier revision
    # gated on `ordered or _consensus_rejected(consensus)` and did exactly that.
    note = "" if submodule_only else consensus_note(consensus)
    if note:
        lines += [note, ""]

    # How the diff was read when it did not fit one prompt. See sharding_note().
    shards = sharding_note(sharding)
    if shards:
        lines += [shards, ""]

    # What was NOT read, immediately under the score. See elision_note().
    elision = elision_note(elided_files)
    if elision:
        lines += [elision, ""]
    coverage_note = preload_coverage_note(preload_coverage)
    if coverage_note:
        lines += [coverage_note, ""]

    # What was found and then withheld. See suppression_note(): this is the
    # difference between "nothing to report" and "everything was filtered",
    # which the body used to render identically.
    suppression = suppression_note(suppressed_findings)
    if suppression:
        lines += [suppression, ""]

    # NO "Files Needing Attention" section. It listed the paths carrying
    # findings — which is a strict subset of the per-file breakdown below, and
    # every one of those paths already appears on the finding that names it.
    # Three renderings of the same fact is not emphasis, it is padding (Erik,
    # 2026-08-10). `files_needing_attention()` stays in the library; nothing
    # here calls it.

    # Defined at render scope, not inside the has-findings branch below:
    # section 3b (out-of-scope findings) calls it on a run where `ordered`
    # is empty, and while that branch was unreachable the nesting was
    # invisible — the informational section only ever rendered because an
    # empty-`ordered` run still fell into the counting branch that defined
    # `emit`. Fixing that fall-through turned the latent scoping bug into
    # an UnboundLocalError, which is how it was found.
    def emit(f: dict, *, permalink: bool) -> None:
        sev = str(f.get("severity", "P2"))
        icon = SEVERITY_ICON.get(sev, "•")
        label = f"{sev} {SEVERITY_LABEL.get(sev, '')}".strip()
        if f.get("security"):
            label += " · 🔒 security"
        if f.get("non_blocking"):
            label += " · ℹ️ non-blocking"
        loc = "_PR-level_"
        if f.get("file") and f.get("line"):
            loc = _md_code(f"{f['file']}:{f['line']}")
            if permalink and repo_slug:
                url = (
                    f"https://github.com/{repo_slug}/blob/"
                    f"{full_sha}/{quote(str(f['file']), safe='/')}#L{f['line']}"
                )
                loc = f"[{loc}]({url})"
        elif f.get("file"):
            loc = _md_code(str(f["file"]))
        # A bold line, not an `###` heading. Each finding used to open a
        # section heading, which on a 6-finding review turned the comment
        # into a table of contents for itself — and rendered as headings
        # *inside* a collapsed <details>, which is worse.
        lines.append(f"**{icon} {label}** — {loc}")
        lines.append("")
        # Escaped for the same reason the change description is: a finding
        # that says "the regex only matches <thinking>…</thinking>" renders
        # as "the regex only matches …" once GitHub eats the tags — which
        # deletes precisely the evidence a reader needs to spot that the
        # finding misread the source. Seen on gptme/gptme#3486, the PR that
        # produced three false P1-security findings from dropped brackets.
        # A comment whose description preserves brackets while its findings
        # silently lose them is worse than either alone.
        body = md_text(str(f.get("body") or "").strip())
        lines.append(body or "_(no detail)_")
        lines.append("")
        if f.get("suggestion"):
            # NOT escaped: a fenced block is already literal, and `&lt;` in
            # a ```suggestion block would be committed verbatim by anyone
            # who clicks "Apply".
            lines.extend(["```suggestion", str(f["suggestion"]).rstrip(), "```", ""])
        if f.get("verification"):
            lines.extend(
                [
                    (
                        "<sub>**How this was verified:** "
                        f"{md_text(str(f['verification']).strip())}</sub>"
                    ),
                    "",
                ]
            )
        # Agreement is signal, not bookkeeping: 3/3 survived every
        # independent sample, 2/3 survived the minimum. Showing it lets a
        # reader weight a finding without knowing how the filter works.
        agreement, total = f.get("agreement"), f.get("passes")
        if isinstance(agreement, int) and isinstance(total, int) and total > 1:
            consensus_line = (
                f"<sub>Consensus: {agreement}/{total} passes agreed"
                f"{agreement_dissent(f)}</sub>"
            )
            # Also show distinct-key consensus when available (measuring whether
            # consensus reflects independent sources vs. resampled passes of the
            # same model+aspect). See ai-reviewer-consensus-independence-key task.
            distinct_agreement = f.get("distinct_key_agreement")
            if isinstance(distinct_agreement, int):
                keys = f.get("distinct_keys", [])
                if isinstance(keys, list) and keys:
                    key_labels = ", ".join(
                        f"<code>{k.split(':')[1]}</code>"
                        if ":" in k
                        else f"<code>{k}</code>"
                        for k in keys
                    )
                    consensus_line += (
                        f" <br/><span style='color:gray;font-size:0.85em'>"
                        f"Distinct keys: {distinct_agreement} "
                        f"({key_labels})</span>"
                    )
            lines.extend([consensus_line, ""])

    # 3. The findings themselves.
    if pointer_bump_only:
        # A clean pass here would be a false clean, which is just a hallucinated
        # finding pointed the other way. All the reviewer saw was
        # `-Subproject commit <old>` / `+Subproject commit <new>`; whether the
        # bump is safe lives in the submodule's commit range, which is not in
        # this diff at all. Say that instead of implying assurance we do not
        # have. Seen on gptme-cloud#850: an 8-second "the diff looks correct"
        # on a one-line pointer change.
        lines += [
            "ℹ️ **Submodule pointer change only — not reviewed.**",
            "",
            (
                "This diff moves a submodule SHA and contains no source "
                "changes, so there is nothing here for me to assess. Whether "
                "the bump is safe depends on the submodule's commit range, "
                "which this diff does not include. Treat this as *not "
                "reviewed*, not as approved."
            ),
            "",
        ]
    elif not ordered and n_suppressed:
        # Every in-scope finding this pass raised was suppressed by policy.
        # No findings section, and no "clean" ✅ — the score above already says
        # N/A and the suppression note names the drop. A note ABOUT the absence
        # ("No findings reported — but not because none were found") is exactly
        # the meta-essay contrib#1434 flagged; silence carries no false signal.
        pass
    elif not ordered:
        # `informational_findings` can be non-empty here. They are out-of-scope
        # by construction, so they never enter `ordered`, the score, or the
        # marker — but they ARE printed below, so "No findings." would be a
        # lie the reader can see through. Falling into the counting branch
        # instead is worse: it renders "**0 findings** ·  — no P0/P1 on this
        # head; the P2s here keep the score at 4/5", a sentence with an empty
        # severity split that describes P2s nobody raised.
        if advisory_findings:
            lines += [
                "✅ **No thread-worthy findings.** Advisory notes follow; they "
                "are retained without opening review threads.",
                "",
            ]
        elif informational_findings:
            lines += [
                "✅ **No findings in this PR's changed files.** What follows is "
                "outside them — not about this change, and not scored.",
                "",
            ]
        else:
            lines += [
                "✅ **No findings.** The diff looks correct to me on this pass.",
                "",
            ]
    else:
        counts: dict[str, int] = {}
        for f in ordered:
            sev = str(f.get("severity", "P2"))
            counts[sev] = counts.get(sev, 0) + 1
        summary = " · ".join(
            f"{SEVERITY_ICON.get(s, '•')} {counts[s]} {s}"
            for s in sorted(counts, key=lambda x: SEVERITY_ORDER.get(x, 9))
        )
        n_sec = sum(1 for f in ordered if f.get("security"))
        if n_sec:
            summary += f" · 🔒 {n_sec} security"
        high = sum(
            1
            for f in ordered
            if not f.get("non_blocking")
            and str(f.get("severity", "P2")) in ("P0", "P1")
        )
        n_nonblocking = sum(1 for f in ordered if f.get("non_blocking"))
        # State what was found, never whether it may be merged (Erik,
        # 2026-08-11). "None blocking, so the findings below are advisory" was
        # a verdict the reviewer has no standing to give. Naming the score
        # consequence keeps the severity split honest without deciding the
        # merge.
        #
        # What this line must NOT say any more: that a P2 "blocks just as hard
        # as a P1". That was true of a score-based gate and stopped being true
        # the same day it was written — contrib#1405 (`f440b61d`, 2026-08-11)
        # made the self-merge gate disposition-based, with
        # AI_REVIEW_BLOCKING_SEVERITIES = {P0, P1} and "surviving P2s never
        # block". A 4/5 is now an acceptable head. Telling an author their P2
        # is merge-blocking manufactures the fix round + CI run + re-review
        # that P2 exists to avoid, which is the whole cost the test-coverage
        # rule in `build_review_prompt` is calibrated against.
        #
        # A round-cap downgraded finding (`non_blocking`) is informational: it
        # is still reported below, but it does not keep the score under 5/5 and
        # must not be described as blocking.
        #
        # No P0/P1-restating clause here any more: the severity breakdown
        # above (e.g. "❌ 1 P1") already says exactly that, and appending
        # "— N at P0/P1" repeated the same count a second time on the same
        # line (gptme-contrib#1621, Erik 2026-09-07 — "shouldn't repeat like
        # that, messy to scan"). The one thing this line still adds beyond the
        # breakdown is the round-cap case, which the breakdown cannot say.
        if n_nonblocking and not high:
            verdict = " — non-blocking (round cap reached)"
        else:
            verdict = ""
        # One line, not three: count, severity split, and the resulting state
        # all belong on the same glance. `<b>`, not `**`: this headline is
        # reused verbatim as an HTML `<summary>` below, and GitHub does not
        # run markdown inside inline HTML on the same line.
        n = len(ordered)
        headline = f"<b>{n} finding{'' if n == 1 else 's'}</b> · {summary}{verdict}"

        inline_findings = [f for f in ordered if id(f) in anchored]
        outside = [f for f in ordered if id(f) not in anchored]

        # The headline IS the <details> title when every finding is inline
        # (the common case). A second "N findings posted inline on the diff"
        # title under it said the same thing twice (Erik, 2026-08-20).
        if inline_findings and not outside:
            lines += [f"<details><summary>{headline}</summary>", ""]
            for f in inline_findings:
                emit(f, permalink=False)
            lines += ["</details>", ""]
        else:
            lines += [headline, ""]
            if inline_findings:
                n_in = len(inline_findings)
                lines += [f"<details><summary><b>{n_in} inline</b></summary>", ""]
                for f in inline_findings:
                    emit(f, permalink=False)
                lines += ["</details>", ""]

        # "Comments outside diff": GitHub only accepts an inline comment on a
        # line that is part of the diff. Anything else would be silently lost,
        # so it is surfaced here in full, with a permalink to the exact line.
        if outside:
            if inline_findings:
                lines += [
                    "### Comments outside the diff",
                    "",
                    (
                        "<sub>These could not be anchored to a line GitHub "
                        "accepts, so they appear only here.</sub>"
                    ),
                    "",
                ]
            else:
                lines += ["### Findings", ""]
            for f in outside:
                emit(f, permalink=True)

    # 3b. Findings that are real but are not about this change: they land in
    #     files the PR never touched, so they must not move the score and must
    #     not enter the marker's `findings` (a P1 there makes the merge gate
    #     demand a disposed thread for a defect this author did not introduce).
    #
    #     They are still shown IN FULL. The previous behaviour published a
    #     count and a policy explanation with the finding bodies deleted, which
    #     is the one outcome that helps nobody: the reader cannot act on it, and
    #     cannot file it as an issue either. Collapsed, so a noisy pass costs a
    #     line rather than a screen.
    if informational_findings and not pointer_bump_only:
        n_info = len(informational_findings)
        lines += [
            (
                f"<details><summary><b>{n_info} out-of-scope finding"
                f"{'' if n_info == 1 else 's'} (outside this PR's changed "
                "files)</b></summary>"
            ),
            "",
            non_blocking_note(informational_findings, "out_of_scope"),
            "",
        ]
        for f in sorted(
            informational_findings,
            key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "P2")), 9),
        ):
            emit(f, permalink=True)
        lines += ["</details>", ""]

    # 3c. P2 claims whose class predicts advice rather than a fix. They remain
    #     visible in one collapsed summary section and in marker evidence, but
    #     never become inline threads or affect the score/header count.
    if advisory_findings and not pointer_bump_only:
        n_adv = len(advisory_findings)
        lines += [
            (
                f"<details><summary><b>{n_adv} advisory finding"
                f"{'' if n_adv == 1 else 's'} (summary-only, not scored)"
                "</b></summary>"
            ),
            "",
            (
                "_These P2 guard, heuristic, trade-off, or documentation claims "
                "are retained for judgment without opening review threads._"
            ),
            "",
        ]
        for f in advisory_findings:
            emit(f, permalink=False)
        lines += ["</details>", ""]

    # 4. The per-file breakdown, below the findings: reference material for
    #    checking what the reviewer read, not the headline.
    if not pointer_bump_only:
        lines += render_files_changed(change_summary, undescribed)

    if history:
        lines += ["<details><summary>Previous review passes</summary>", ""]
        # This one stays a table: four short, uniform columns with no wrapping
        # prose, which is the case tables are actually good at. Entries written
        # before scoring existed have no `score` key and show `?`.
        lines += [
            "| commit | score | findings | engine | when |",
            "| --- | --- | --- | --- | --- |",
        ]
        for h in history[-10:]:
            past = h.get("score")
            lines.append(
                f"| `{h.get('sha', '?')}` | {f'{past}/5' if past else '?'} "
                f"| {h.get('findings', '?')} "
                f"| {h.get('engine', '?')} | {h.get('at', '?')} |"
            )
        lines += ["", "</details>", ""]

    # 5. Footer, small text, no rule above it. One line of provenance with a
    #    link to the public reviewer doc; the maintainer triggers live behind
    #    a <details> so they are discoverable without being read every time
    #    (Erik, 2026-08-20: the always-visible paragraph was noise).
    #    Both trigger paths skip comments authored by the reviewer itself, so
    #    naming them here cannot self-trigger.
    lines += [
        (
            f"<sub>Reviewed `{head_sha}` · `{model}` · `{engine}` engine · "
            f"{elapsed:.0f}s · [about this reviewer]({REVIEWER_DOC_URL})</sub>"
        ),
        "",
        "<details><summary><sub>Maintainer commands</sub></summary>",
        "",
        maintainer_commands_line(_io.REVIEWER_IDENTITY.login),
        "",
        "</details>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checkout helpers (writer-port slice 2d; the agent engine stays host-side)
#
# ``run`` and ``CHECKOUT_ROOT`` resolve through ``_io`` so a host's patches and
# checkout root reach calls made in here. ``GPTME_REVIEW_CHECKOUT`` still
# overrides the per-repo path; ownership is verified either way.
# ---------------------------------------------------------------------------


def read_checkout_file(checkout: Path, path: str) -> str | None:
    try:
        full = (checkout / path).resolve()
        if not full.is_relative_to(checkout.resolve()):
            return None
        return full.read_text(errors="replace")
    except (OSError, RuntimeError):
        return None


def _slug_from_remote_url(url: str) -> str | None:
    """Reduce a git remote URL to a lowercased ``owner/name`` slug.

    Handles the three shapes git remotes actually take here: scp-style
    (``git@github.com:owner/name.git``), URL-style (``https://host/owner/name``,
    ``ssh://git@host/owner/name.git``), and bare paths. Returns None when the URL
    does not carry at least two path segments.
    """
    text = url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if not text:
        return None

    if "://" in text:
        rest = text.split("://", 1)[1]
        rest = rest.rsplit("@", 1)[-1]  # strip any userinfo
        parts = rest.split("/")[1:]  # drop host
    elif ":" in text and not text.startswith("/"):
        parts = text.partition(":")[2].split("/")
    else:
        parts = text.split("/")

    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:]).lower()


def remote_owning_repo(checkout: Path, repo: str) -> str | None:
    """Name of the remote in ``checkout`` that points at ``repo``, if any.

    ``origin`` wins when several remotes match; otherwise the first in git's
    listing order. Returns None when no remote owns the repo — the caller must
    refuse rather than fall back to a remote that does not have the SHA.
    """
    listing = _io.run(["git", "remote", "-v"], cwd=checkout)
    if listing.returncode != 0:
        return None

    want = repo.strip().lower()
    matches: list[str] = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        name, url = fields[0], fields[1]
        if _slug_from_remote_url(url) == want and name not in matches:
            matches.append(name)

    if not matches:
        return None
    return "origin" if "origin" in matches else matches[0]


def _path_component(text: str) -> str:
    """Make one path-safe component out of a repo owner/name."""
    return (
        "".join(c if (c.isalnum() or c in "._-") else "-" for c in text).strip("-.")
        or "unknown"
    )


def review_checkout_for(repo: str) -> Path:
    """Path of the agent-engine review checkout dedicated to ``repo``.

    ``GPTME_REVIEW_CHECKOUT`` still overrides the path, but it is no longer a
    licence to review the wrong repository: prepare_agent_checkout() verifies
    that whatever checkout it is handed actually has a remote owning ``repo``.
    """
    override = os.environ.get("GPTME_REVIEW_CHECKOUT")
    if override:
        return Path(override)
    owner, _, name = repo.partition("/")
    slug = (
        f"{_path_component(owner)}-{_path_component(name)}"
        if owner and name
        else _path_component(repo)
    )
    root: Path = _io.CHECKOUT_ROOT
    return root / f"{slug}-review"


def build_cross_repo_sibling_note(repo: str) -> str:
    """Generate a context note about available sibling repo checkouts.

    For agent-engine reviews, this tells the session where the sibling repos
    live on disk so it can use its read tools to check for duplication (T2c-i)
    or contract drift (T2c-ii).  Returns an empty string when *repo* is not in
    a known family or no sibling checkouts are present.
    """
    family_entry = repo_family(repo)
    if family_entry is None:
        return ""
    family_name, family_type, sibling_repos = family_entry
    siblings_on_disk: list[tuple[str, Path]] = []
    for sibling in sorted(sibling_repos):
        if sibling == repo:
            continue
        path = review_checkout_for(sibling)
        if path.exists():
            siblings_on_disk.append((sibling, path))
    if not siblings_on_disk:
        return ""
    if family_type == "shared-tooling":
        header = (
            f"## Cross-repo context — {family_name} shared-tooling family (T2c-i)\n\n"
            "This PR is in a shared-tooling family.  Use your read tools to search "
            "the sibling checkouts below when checking for duplication or wrong-repo "
            "placement.  Generic helpers belong in gptme-contrib, not in an agent "
            "brain repo (bob, alice).\n"
        )
    else:
        header = (
            f"## Cross-repo context — {family_name} component-split system (T2c-ii)\n\n"
            "This PR is in a component-split system.  Use your read tools to search "
            "the sibling checkouts below when checking for API contract drift.  "
            "aw-server and aw-server-rust implement the same REST API; a contract "
            "change in one must be mirrored in the other.\n"
        )
    lines = [header, "Available sibling checkouts:\n"]
    for sibling, path in siblings_on_disk:
        lines.append(f"- `{sibling}` → `{path}`")
    return "\n".join(lines)


def acquire_agent_checkout_lock(checkout: Path, *, wait_secs: float = 2700.0):
    """Take the exclusive host-local lock on one agent-review checkout.

    The agent engine reads files from ONE shared per-repo checkout and
    ``prepare_agent_checkout`` moves its HEAD, so two reviews of the same repo
    (two PRs in one sweep, or a sweep plus a PM slot) must not overlap: the
    second ``git checkout`` rewrites files under the first session's read tool,
    and two first-time ``gh repo clone``s into the same path collide
    (``failed to run git: exit status 128``). Both happened on 2026-10-02 in the
    bounded subscription-overflow path (gptme-cloud#1105 clone race;
    gptme-cloud#1103 lost its tree to a PM review of #1061 mid-session).

    Hold the returned file open for the whole prepare + review; closing it
    releases the lock. Polls rather than blocking forever so a wedged holder
    surfaces as a ReviewError instead of a hung sweep slot.
    """
    lock_path = checkout.parent / f"{checkout.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    deadline = time.monotonic() + wait_secs
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ReviewError(
                    f"agent review checkout {checkout} stayed locked for "
                    f"{wait_secs:.0f}s — another review of this repo is wedged"
                ) from None
            time.sleep(2.0)


def prepare_agent_checkout(repo: str, head_sha: str, checkout: Path) -> None:
    """Put ``repo``'s dedicated review worktree at the exact commit reviewed.

    A missing checkout is cloned, not refused. The engine lives under
    ``/tmp/worktrees`` and nothing else creates that path, so "clone it there
    first" was a manual step that never happened: the 2026-09-07 live probe
    found the engine dead at this line, three weeks after its revive task
    closed. A path that self-creates cannot rot the same way.
    """
    if not (checkout / ".git").exists():
        clone = _io.run(
            ["gh", "repo", "clone", repo, str(checkout), "--", "--quiet"],
            timeout=600,
        )
        if clone.returncode != 0:
            raise ReviewError(
                f"agent review checkout for {repo} does not exist and cloning "
                f"it to {checkout} failed: {clone.stderr[-300:]}"
            )
    status = _io.run(["git", "status", "--porcelain"], cwd=checkout)
    if status.returncode != 0:
        raise ReviewError(
            f"cannot inspect agent review checkout: {status.stderr[-300:]}"
        )
    if status.stdout.strip():
        raise ReviewError(f"agent review checkout is dirty: {checkout}")
    # Never fetch a SHA from a remote that does not own the repo being reviewed.
    # A wrong-repo review is undetectable downstream — the quotes match the files
    # they were read from — so this refuses rather than warning.
    remote = remote_owning_repo(checkout, repo)
    if remote is None:
        raise ReviewError(
            f"agent review checkout {checkout} has no remote pointing at {repo}; "
            "refusing to review one repo against another's objects"
        )
    fetch = _io.run(
        ["git", "fetch", "--quiet", remote, head_sha],
        cwd=checkout,
        timeout=120,
    )
    if fetch.returncode != 0:
        raise ReviewError(f"cannot fetch review head {head_sha}: {fetch.stderr[-300:]}")
    switch = _io.run(
        ["git", "checkout", "--quiet", "--detach", head_sha],
        cwd=checkout,
        timeout=60,
    )
    if switch.returncode != 0:
        raise ReviewError(
            f"cannot check out review head {head_sha}: {switch.stderr[-300:]}"
        )
    actual = _io.run(["git", "rev-parse", "HEAD"], cwd=checkout)
    if actual.returncode != 0 or actual.stdout.strip() != head_sha:
        raise ReviewError(
            f"agent review checkout mismatch: expected {head_sha}, "
            f"got {actual.stdout.strip() or 'unknown'}"
        )


# ---------------------------------------------------------------------------
# Static pre-flight finding generators (rules 1a, 1b, 1c, 7; writer-port 2d)
# ---------------------------------------------------------------------------
# Mechanical checks that run on every PR regardless of engine. They carry one
# sentence of Erik's rationale so the knowledge lives in the tool.
#
# Severity policy:
#   P1 (hard block) — rules 1a, 1b, 1c: things Erik explicitly called out
#   P2 (advisory)   — rule 7: Erik merged all such PRs but asked for tests
#
# Host-specific checks (Bob's rule 8, journal append-only) come from
# ``WriterHost.extra_static_checks``. Results are injected into the main
# findings list AFTER the scope filter and BEFORE suppression.


def _static_finding(
    *,
    title: str,
    body: str,
    file: str,
    severity: str,
    quote: str = "",
    verification: str,
    finding_class: str = "bug",
    suggestion: str = "",
    line: int | None = None,
) -> dict:
    """Canonical shape for a static preflight finding."""
    f: dict = {
        "title": title,
        "body": body,
        "file": file,
        "security": False,
        "quote": quote,
        "verification": verification,
        "class": normalize_finding_class(finding_class),
        "severity": severity,
        "suggestion": suggestion,
        "aspect": "preflight",
        "static": True,  # identifies static findings in tests and logs
    }
    if line is not None:
        f["line"] = line
    return f


_SUBPROJECT_COMMIT_RE = re.compile(
    r"^[+-]Subproject commit ([0-9a-f]{7,40})\s*$", re.MULTILINE
)
_GITLINK_INDEX_RE = re.compile(
    r"^index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40}) 160000$", re.MULTILINE
)
# `owner/repo#123` and markdown links to github.com/owner/repo/...
_BODY_REPO_REF_RE = re.compile(
    r"(?:github\.com/|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"|(\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#\d+"
)


def _gitlink_shas_from_diff(diff: str) -> set[str]:
    """SHAs this PR actually moves as submodule gitlinks."""
    shas: set[str] = set()
    for m in _SUBPROJECT_COMMIT_RE.finditer(diff or ""):
        shas.add(m.group(1).lower())
    for m in _GITLINK_INDEX_RE.finditer(diff or ""):
        shas.add(m.group(1).lower())
        shas.add(m.group(2).lower())
    return shas


def _sha_matches_any(cited: str, candidates: set[str]) -> bool:
    """True when `cited` is a prefix or extension of a candidate (min 7 chars)."""
    cited = cited.lower()
    if len(cited) < 7:
        return False
    for candidate in candidates:
        overlap = min(len(cited), len(candidate))
        if overlap >= 7 and (
            candidate.startswith(cited) or cited.startswith(candidate)
        ):
            return True
    return False


def _repos_cited_in_body(pr_body: str, parent_repo: str) -> list[str]:
    """GitHub owner/repo refs mentioned in the PR body, excluding the parent."""
    found: list[str] = []
    seen: set[str] = {parent_repo.lower()}
    for m in _BODY_REPO_REF_RE.finditer(pr_body or ""):
        repo = next(g for g in m.groups() if g)
        key = repo.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(repo)
    return found


def _commit_exists(repo: str, sha: str) -> bool:
    check: subprocess.CompletedProcess = _io.run(
        ["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"]
    )
    return check.returncode == 0


def _check_cited_shas(
    pr_body: str,
    repo: str,
    touched_files: Collection[str],
    diff: str = "",
) -> list[dict]:
    """Rule 1a — every SHA cited in the PR body must exist.

    Erik: 'I don't actually see the follow-up commit you claimed' (gptme#2851).

    Superproject-only lookup false-P1'd submodule pins (gptme-cloud#902, #912):
    the cited SHA is the gitlink target in gptme/gptme, not a commit of
    gptme-cloud. Accept a citation when it is the gitlink this PR moves, or
    when it exists in a repo the body explicitly names (`owner/repo#N`).
    """
    # Match "fixed in <sha>", "merged <sha>", "commit <sha>", or bare sha in backticks.
    # Only scan short/medium SHAs (7-12 chars) in "fixed in"/"see"/"commit" context
    # to avoid flagging 40-char SHAs that are clearly full refs or part of prose.
    # `(?<![\w-])` so `squash-merged <40-char sha>` does not match `merged`
    # (gptme-cloud#902: hyphen is a non-word char, so `\\bmerged` still fired).
    # `(?![0-9a-f])` keeps a 40-char SHA from being truncated to its first 12
    # hex chars: the lookup then ran on a prefix that 404s when the real SHA
    # belongs to a submodule or another repo — the exact false P1 the gitlink
    # exemption below was added for.
    _sha = r"`?([0-9a-f]{7,12})`?(?![0-9a-f])"
    pattern = re.compile(
        r"(?:"
        r"(?<![\w-])fixed\s+in\s+" + _sha + r""
        r"|(?<![\w-])merged\s+(?:as\s+)?" + _sha + r""
        r"|(?<![\w-])(?:commit|sha)\s*:?\s*" + _sha + r""
        r"|(?<![\w-])follow.up\s+commit\s+" + _sha + r""
        r")",
        re.IGNORECASE,
    )
    gitlinks = _gitlink_shas_from_diff(diff)
    extra_repos = _repos_cited_in_body(pr_body, repo)
    results: list[dict] = []
    seen: set[str] = set()
    for m in pattern.finditer(pr_body):
        # `re.IGNORECASE` admits A-F; git only resolves lowercase hex.
        sha = next(g for g in m.groups() if g).lower()
        if sha in seen:
            continue
        seen.add(sha)
        if _sha_matches_any(sha, gitlinks):
            continue
        if _commit_exists(repo, sha):
            continue
        if any(_commit_exists(other, sha) for other in extra_repos):
            continue
        anchor = next(iter(touched_files), "")
        results.append(
            _static_finding(
                title=f"Cited commit `{sha}` not found in {repo}",
                body=(
                    f"The PR body references commit `{sha}` but that SHA was not found "
                    f"in `{repo}`"
                    + (
                        f", its gitlink targets, or cited remotes {extra_repos}"
                        if extra_repos
                        else ""
                    )
                    + ". "
                    "Erik's feedback: \"I don't actually see the follow-up commit you "
                    'claimed" (gptme#2851). '
                    "Verify the SHA is reachable on the head branch, is the gitlink "
                    "this PR moves, or correct the reference."
                ),
                file=anchor,
                severity="P1",
                quote=m.group(0)[:80],
                verification="static preflight: GitHub API commit lookup (rule 1a)",
                suggestion=(
                    f"Run `gh api repos/{repo}/commits/{sha}` to verify, "
                    "or remove/correct the reference."
                ),
            )
        )
    return results


#: ``gh pr checks --json state`` values (check-run conclusions/statuses and
#: commit-status states). Every state is classified; anything else is unknown.
_FAILED_CHECK_STATES = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
_NONFAILING_CHECK_STATES = frozenset(
    {
        "SUCCESS",
        "NEUTRAL",
        "SKIPPED",
        "STALE",
        "PENDING",
        "QUEUED",
        "IN_PROGRESS",
        "WAITING",
        "REQUESTED",
        "EXPECTED",
    }
)


def _check_unrelated_ci_claim(
    pr_body: str,
    repo: str,
    number: int,
    touched_files: Collection[str],
) -> list[dict]:
    """Rule 1b — 'unrelated CI failure' claims require proof of a disjoint path filter.

    Erik: '"unrelated to these changes"? I don't think so' (gptme#3169).
    """
    # Detect phrases like "unrelated CI", "flaky test", "known flake".
    #
    # A bare "unrelated" is NOT a CI claim — prose like "unrelated to the version
    # bump" matched the old catch-all and produced a false P1 on a PR whose
    # checks were green (ActivityWatch/activitywatch#1472, 2026-09-27). Require
    # the word to be tied to CI context: an explicit flake phrase, an
    # "unrelated to (these) changes" claim, or "unrelated" within a short span
    # of a CI term.
    _ci_term = (
        r"(?:ci|test|tests|failure|failures|check|checks|job|jobs|workflow|"
        r"build|flake|flakiness|timeout)"
    )
    claim_re = re.compile(
        r"\b(?:known\s+flak(?:e|y)|flaky\s+(?:test|tests|ci)|"
        r"intermittent\s+(?:test|tests|ci|fail(?:ure)?s?))\b"
        rf"|\bunrelated\b[^.\n]{{0,40}}?\b{_ci_term}\b"
        rf"|\b{_ci_term}\b[^.\n]{{0,40}}?\bunrelated\b"
        r"|\bunrelated\s+to\s+(?:these|the|this|my|our)\s+(?:changes|change|pr|diff|commit|commits)\b",
        re.IGNORECASE,
    )
    if not claim_re.search(pr_body):
        return []

    # Check if there are actually failing CI checks
    ci_raw = _io.gh_json(
        ["pr", "checks", str(number), "--repo", repo, "--json", "name,state"]
    )
    if not isinstance(ci_raw, list) or any(
        not isinstance(c, dict)
        or not isinstance(c.get("name"), str)
        or not c["name"]
        or not isinstance(c.get("state"), str)
        or not c["state"]
        for c in ci_raw
    ):
        raise ReviewError(f"cannot verify CI checks for {repo}#{number} (rule 1b)")
    failing = [c for c in ci_raw if c["state"] in _FAILED_CHECK_STATES]
    unknown = sorted(
        {c["state"] for c in ci_raw} - _FAILED_CHECK_STATES - _NONFAILING_CHECK_STATES
    )
    if unknown and not failing:
        # An unrecognised state cannot establish that the claim is moot, so fail
        # closed. It does not, however, invalidate positive evidence from a known
        # failing check — that path still reports the failure below (and names the
        # unknown states there) instead of aborting an otherwise-usable check.
        raise ReviewError(
            f"cannot verify CI checks for {repo}#{number} (rule 1b): "
            f"unknown check state(s) {', '.join(unknown)}"
        )
    if not failing:
        return []  # Claim is moot — no failing checks

    check_names = ", ".join(f"`{c['name']}`" for c in failing[:3])
    unknown_note = (
        f" Unrecognised check state(s) {', '.join(unknown)} are also present and "
        "could not be classified; they are surfaced here rather than dropped."
        if unknown
        else ""
    )
    anchor = next(iter(touched_files), "")
    return [
        _static_finding(
            title="Unverified 'unrelated CI failure' claim",
            body=(
                f"The PR body claims a CI failure is unrelated or flaky, but "
                f"{len(failing)} check(s) are currently failing: {check_names}. "
                "Erik's feedback: '\"unrelated to these changes\"? I don't think so' "
                "(gptme#3169). "
                "A 'flaky' or 'unrelated' claim is accepted only when the failing "
                "job's path filter is demonstrably disjoint from this diff. "
                "State the disjoint filter explicitly, or fix the CI."
                f"{unknown_note}"
            ),
            file=anchor,
            severity="P1",
            verification="static preflight: CI status + unrelated-claim scan (rule 1b)",
            suggestion=(
                "List the failing job's `paths:` / `paths-ignore:` filter and show "
                "it does not overlap with the changed files in this diff."
            ),
        )
    ]


# Every alternative is anchored to a path segment. An unanchored
# ``test_[^/]+$`` matched the *substring* `test_` anywhere in a path, so a
# production file like ``src/latest_compile.py`` counted as a test file and
# silently excused both rule 7 (no regression test) and rule 1c (visual diff
# needs a screenshot).
_TEST_FILE_RE = re.compile(
    r"^tests?/|/tests?/|(?:^|/)test_[^/]+$|[^/]+_test\.[^/]+$|[^/]+\.test\.[^/]+$|[^/]+\.spec\.[^/]+$",
    re.IGNORECASE,
)

# Rust keeps unit tests inline (``#[cfg(test)]`` modules in ``src/``), so an
# added test attribute counts as a test even when no path looks like one.
# Horizontal whitespace only: ``\s`` matches newlines, which let an added
# empty line bridge onto the next context line's existing ``#[test]``.
_ADDED_INLINE_TEST_RE = re.compile(
    r"^\+(?!\+\+)[ \t]*#\[(?:cfg\(test\)|(?:tokio::)?test\b)", re.MULTILINE
)


_VISUAL_PATH_RE = re.compile(
    r"\.(tsx|css|scss|less|svelte)$|/(tui|webui|ui|frontend|components)/",
    re.IGNORECASE,
)
_SCREENSHOT_RE = re.compile(r"!\[|<img\s|screenshot|before.{0,10}after", re.IGNORECASE)


# A diff line that actually changes rendering. A component file can be touched
# by a data-only change (an extra field on an object literal) that renders
# nothing a screenshot could show, so the path alone is not proof of a visual
# change. Measured FP: gptme/gptme-cloud#930 — a single `organization_id: null`
# line in `Instances.tsx` demanded an impossible screenshot for 8 review rounds.
# Session 49dc predicted this sibling of the gptme#3741 test-file exemption.
_VISUAL_CHANGE_RE = re.compile(
    # `<[A-Za-z/]` must not fire on TypeScript generics/type annotations.
    # Measured FP: gptme/gptme#4428 — a data-flow-only change to
    # ConversationItem.tsx matched `Pick<ConversationSummary, ...>`,
    # `Promise<void>` and a `<Computed>` mention inside a comment, so rule 1c
    # demanded a screenshot of a diff that renders nothing. A JSX tag is never
    # glued to a preceding identifier character; a generic always is.
    r"(?<![A-Za-z0-9_$])<[A-Za-z/]|className=|class=|style=|color|bg-|background|"
    r"width:|height:|margin|padding|font|border|z-|display:|opacity|justify|"
    r"align-|gap-|grid-|flex|shadow|rounded",
    re.IGNORECASE,
)

_COMMENT_LINE_RE = re.compile(r"^\s*(?://|/\*|\{/\*)")

# A block-comment continuation line starts with `*`, but so does a CSS
# universal-selector rule. `* { color: red }` is a real visual change and
# `_VISUAL_CHANGE_RE` would flag it, so the bare-`*` skip must not swallow it:
# a continuation line is prose and carries no rule body (`{`), a CSS rule does.
_COMMENT_CONTINUATION_RE = re.compile(r"^\s*\*(?:\s|$)")

# a11y-only props are not screenshotable. Measured FP: gptme/gptme-cloud#946 —
# an aria-label / aria-hidden hamburger-toggle change raised a blocking P1
# because `<Button` / `className=` still matched `_VISUAL_CHANGE_RE`.
_A11Y_ATTR_RE = re.compile(
    r"""
    \s+
    (?:aria-[\w-]+|role|tabIndex|tabindex)
    =
    (?:\{[^{}]*\}|"[^"]*"|'[^']*')
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _normalize_visual_payload(lines: list[str]) -> str:
    """Join +/- lines into a whitespace-stable string with a11y attrs removed.

    Lets an aria-label-only reformat of the same JSX compare equal to the
    pre-change markup, so rule 1c does not demand an impossible screenshot.
    """
    text = "\n".join(raw[1:] for raw in lines)
    text = _A11Y_ATTR_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+(/?>)", r"\1", text)
    return text.strip()


def _is_comment_line(line: str) -> bool:
    """True for a source line that renders nothing (a comment).

    `*` is ambiguous: it starts a block-comment continuation (`* prose`) and
    also a CSS universal-selector rule (`* { color: red }`). Only treat it as
    a comment when the line carries no rule body, so a universal-selector
    change is not silently exempted from the rule 1c screenshot requirement.
    """
    if _COMMENT_LINE_RE.match(line):
        return True
    return bool(_COMMENT_CONTINUATION_RE.match(line)) and "{" not in line


def _diff_changes_rendering(file_diff: str) -> bool:
    """True when a visual file's hunk has any rendering-affecting change.

    Walks only the added/removed lines of one file's diff section. A hunk that
    only adds files, fields, or non-visual props is data, not UI — nothing a
    screenshot could show. A11y-attribute-only edits (aria-*, role, tabIndex)
    of the same markup are treated the same way.
    """
    added: list[str] = []
    removed: list[str] = []
    in_hunk = False
    for raw in file_diff.splitlines():
        if raw.startswith("@@"):
            in_hunk = True
            continue
        # `+++`/`---` before the first hunk are file headers. Inside a hunk the
        # same prefix is hunk content: an added line starting with `++` (e.g.
        # the CSS custom property `++my-var: 1px`) renders as `+++my-var`, and
        # skipping it would exempt a real visual change from rule 1c.
        if not in_hunk and raw.startswith(("+++", "---")):
            continue
        if raw.startswith("+"):
            bucket = added
        elif raw.startswith("-"):
            bucket = removed
        else:
            continue
        # Comment lines render nothing; prose like `// Legend <Computed>` must
        # not be mistaken for markup.
        if _is_comment_line(raw[1:]):
            continue
        bucket.append(raw)
    if added or removed:
        if _normalize_visual_payload(added) == _normalize_visual_payload(removed):
            return False
    for raw in added + removed:
        if _VISUAL_CHANGE_RE.search(raw):
            return True
    return False


def _diff_deletes_whole_file(file_diff: list[str]) -> bool:
    """True when this file's diff section deletes the file entirely.

    A deleted file renders nothing, so a screenshot cannot show its removal.
    Any real UI impact from deleting a component appears in the files that stop
    importing it, which are checked separately. Measured FP: gptme/gptme#4290 —
    19 unused shadcn wrappers deleted and rule 1c demanded a screenshot of a
    deletion, producing a P1 that could not even be anchored (the deleted
    file's line 1 is not in the new side of the diff), so it was undisposable
    by construction and deadlocked the self-merge gate.
    """
    return any(raw.rstrip() == "+++ /dev/null" for raw in file_diff)


def _diff_section_path(section: list[str]) -> str | None:
    """The repository-relative new-side path of one ``diff --git`` section.

    Read from the section's ``+++ b/<path>`` line, not by splitting the
    ``diff --git a/<old> b/<new>`` header. Git does not escape spaces in
    paths, so a path that itself contains `` b/ `` (legal) makes that split
    land mid-path; the section is then keyed under a name no caller looks up
    and a genuinely visual change is silently exempted from rule 1c. Only the
    header region is scanned — an added line whose content starts with
    ``+ b/`` renders as ``+++ b/`` inside a hunk.

    A deletion has ``+++ /dev/null``, so it falls back to the ``--- a/<path>``
    name that `_diff_deletes_whole_file` expects.
    """
    new_path: str | None = None
    old_path: str | None = None
    for raw in section:
        if raw.startswith("@@"):
            break
        if raw.startswith("+++ b/"):
            new_path = raw[len("+++ b/") :]
        elif raw.startswith("--- a/"):
            old_path = raw[len("--- a/") :]
    chosen = new_path if new_path is not None else old_path
    if chosen is None:
        return None
    return chosen.split("\t", 1)[0].rstrip() or None


def _check_visual_screenshot(
    pr_body: str, diff: str, touched_files: Collection[str]
) -> list[dict]:
    """Rule 1c — visual diffs need a before/after screenshot.

    Erik: 'I merged it, but it's STILL gray bg!' (gptme#3321, #3334, #3461).
    """
    # Test files match the visual patterns (e.g. `Foo.test.tsx`) but render
    # nothing a screenshot could show, so they never satisfy this rule.
    # Python sources match `/(tui|...)/` (gptme/tui/app.py) the same way, but
    # they do not render a screenshotable page. Measured FP: gptme/gptme#3750
    # (metadata-only TUI drain) blocked self-merge on an unsatisfiable P1.
    # Session 49dc predicted this sibling and left it until it showed up.
    visual_files = [
        f
        for f in touched_files
        if _VISUAL_PATH_RE.search(f)
        and not _TEST_FILE_RE.search(f)
        and not f.endswith(".py")
    ]
    if not visual_files:
        return []
    if _SCREENSHOT_RE.search(pr_body):
        return []

    # Some component files are touched only by data changes (a new field on an
    # object literal, a renamed prop without any render impact). Those render
    # nothing a screenshot could show, so they are exempt the same way test
    # files are. Split the diff by file to check each visual file's own hunks.
    sections: list[list[str]] = []
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            sections.append([raw])
        elif sections:
            sections[-1].append(raw)

    file_diffs: dict[str, list[str]] = {}
    for section in sections:
        path = _diff_section_path(section)
        if path is not None:
            file_diffs.setdefault(path, []).extend(section)

    visual_files = [
        f
        for f in visual_files
        if not _diff_deletes_whole_file(file_diffs.get(f, []))
        and _diff_changes_rendering("\n".join(file_diffs.get(f, [])))
    ]
    if not visual_files:
        return []

    sample = ", ".join(f"`{f}`" for f in visual_files[:3])
    extra = f" (+ {len(visual_files) - 3} more)" if len(visual_files) > 3 else ""
    return [
        _static_finding(
            title="Visual changes need a before/after screenshot",
            body=(
                f"This PR modifies visual/UI files ({sample}{extra}) "
                "but the PR body contains no screenshot or before/after image. "
                "Erik's feedback: 'I merged it, but it's STILL gray bg!' "
                "(gptme#3321) and 'Add a before/after screenshot' (gptme#3334, #3461). "
                "Attach a screenshot showing the rendered change."
            ),
            file=visual_files[0],
            severity="P1",
            line=1,
            verification="static preflight: visual-file diff check (rule 1c)",
            suggestion="Add a Markdown image to the PR body: `![Before](url) ![After](url)`",
        )
    ]


def _check_fix_without_test(
    pr_title: str, touched_files: Collection[str], diff: str = ""
) -> list[dict]:
    """Rule 7 — fix(...) PR with zero test files is advisory P2.

    Erik: 'where is the repro & fixes they are supposed to catch' (gptme#3441),
    'that measurement should come with a regression test' (gptme#3446).
    Advisory because Erik merged every such PR, but asked for tests each time.
    """
    # ``!`` marks a breaking change (Conventional Commits); it is still a fix.
    if not re.match(r"^fix(\(.*?\))?!?:", pr_title.strip(), re.IGNORECASE):
        return []
    if any(_TEST_FILE_RE.search(f) for f in touched_files):
        return []
    # Attributes in documentation examples or other languages are not Rust tests.
    for section in re.split(r"(?m)^diff --git ", diff)[1:]:
        lines = section.splitlines()
        path = _diff_section_path(lines)
        if path is not None and path.endswith(".rs"):
            if _ADDED_INLINE_TEST_RE.search("\n".join(lines)):
                return []
    anchor = next(iter(touched_files), "")
    return [
        _static_finding(
            title="fix PR has no regression test",
            body=(
                "This is a `fix(...)` PR but no test files are included in the diff. "
                "Erik's feedback: 'where is the repro & fixes they are supposed to catch' "
                "(gptme#3441), 'that measurement should come with a regression test' (gptme#3446). "
                "Add a test that would have caught this bug. "
                "(Advisory: Erik merged all such PRs but consistently requested tests.)"
            ),
            file=anchor,
            severity="P2",
            finding_class="guard",
            verification="static preflight: fix-commit + touched-files scan (rule 7)",
            suggestion=(
                "Add a test file that reproduces the bug before the fix and "
                "passes after it."
            ),
        )
    ]


def static_preflight_findings(
    *,
    pr_title: str,
    pr_body: str,
    diff: str,
    touched_files: Collection[str],
    repo: str,
    number: int,
    host: DefaultWriterHost | None = None,
) -> list[dict]:
    """Collect all mechanical P1/P2 findings that don't require LLM judgment.

    Implements rules 1a, 1b, 1c and 7 (plus host checks) from the 90-day merge-path census
    (knowledge/strategic/2026-09-07-auto-merge-analysis.md §3).

    Rules 1a, 1b, 1c → P1 hard block
    Rule 7              → P2 advisory

    Each finding carries one sentence of Erik's rationale so the block reason
    is self-explanatory in the PR comment without requiring a lookup.

    ``host.extra_static_checks`` adds host policy (Bob's journal append-only
    rule 8); ``None`` means :class:`DefaultWriterHost`, which adds none.
    """
    if host is None:
        host = DefaultWriterHost()
    results: list[dict] = []
    results.extend(_check_cited_shas(pr_body, repo, touched_files, diff))
    results.extend(_check_unrelated_ci_claim(pr_body, repo, number, touched_files))
    results.extend(_check_visual_screenshot(pr_body, diff, touched_files))
    results.extend(_check_fix_without_test(pr_title, touched_files, diff))
    results.extend(host.extra_static_checks(diff, touched_files))
    return results


# ---------------------------------------------------------------------------
# Engine call helpers (writer-port slice 3a)
#
# One review of a diff on the OpenRouter engine, its sharded fan-out and the
# preload that feeds both. Calls between members, and the seams a host patches
# (``review``, ``fetch_submodule_context``, ``fetch_file_preload``,
# ``drop_unverified_absence_findings``), resolve through ``_io``. The
# subscription engine and the run ledger are host hooks on ``_io.WRITER_HOST``.
# ---------------------------------------------------------------------------


def preload_files(
    args: argparse.Namespace, pr: Mapping[str, object], ranked_paths: list[str]
) -> tuple[dict[str, str], set[str], dict[str, int]]:
    """Post-change contents for ``ranked_paths``: (contents, fully read, coverage)."""
    if args.no_preload:
        return (
            {},
            set(),
            {
                "files_in_diff": len(ranked_paths),
                "files_preloaded": 0,
                "files_truncated": len(ranked_paths),
            },
        )
    # Preserve the legacy ``fetch_file_contents`` patch seam while production
    # uses the coverage-aware preload result.
    original = getattr(_io, "_ORIGINAL_FETCH_FILE_CONTENTS", fetch_file_contents)
    if _io.fetch_file_contents is not original:
        contents = _io.fetch_file_contents(
            args.repo, str(pr.get("headRefOid")), ranked_paths
        )
        fully = set(contents)
        return (
            contents,
            fully,
            {
                "files_in_diff": len(ranked_paths),
                "files_preloaded": len(fully),
                "files_truncated": len(ranked_paths) - len(fully),
            },
        )
    preload = _io.fetch_file_preload(args.repo, str(pr.get("headRefOid")), ranked_paths)
    return preload.contents, set(preload.fully_preloaded), dict(preload.coverage)


def _stage_timeout(args: argparse.Namespace, requested: float, stage: str) -> float:
    """Return this stage's share of the shared deadline, if configured."""
    lifecycle.stage(stage)
    deadline = getattr(args, "deadline", None)
    if isinstance(deadline, ReviewDeadline):
        return deadline.cap(requested, stage)
    return float(requested)


def llm_review_call(
    args: argparse.Namespace,
    pr: Mapping[str, object],
    *,
    model: str,
    key: str,
    diff: str,
    contents: dict[str, str],
    preload_coverage: Mapping[str, int] | None,
    note: str,
    label: str = "",
) -> tuple[list[dict], dict]:
    """One review of ``diff`` on the configured route, logged for measurement.

    ``--engine subscription`` goes to the panel; everything else is the
    OpenRouter path, wrapped so its wall time, cost and raw findings land in the
    same ledger as the panel's (``state/ai-review-runs.jsonl``).
    """
    if args.engine == "subscription":
        sub_result: tuple[list[dict], dict] = _io.WRITER_HOST.subscription_review_call(
            args, pr, diff=diff, contents=contents, note=note, label=label
        )
        return sub_result
    started = time.monotonic()
    sha = str(pr.get("headRefOid") or "")
    model_name = model.removeprefix("openrouter/")
    try:
        findings, usage = _io._openrouter_review_call(
            args,
            pr,
            model=model,
            key=key,
            diff=diff,
            contents=contents,
            preload_coverage=preload_coverage,
            note=note,
            label=label,
        )
    except ReviewError as exc:
        if not args.dry_run and not label:
            _io.WRITER_HOST.log_run(
                repo=args.repo,
                number=args.number,
                sha=sha,
                model=model_name,
                usage={},
                wall_s=time.monotonic() - started,
                findings=[],
                error=str(exc),
            )
        raise
    if not args.dry_run and not label:
        _io.WRITER_HOST.log_run(
            repo=args.repo,
            number=args.number,
            sha=sha,
            model=model_name,
            usage=usage,
            wall_s=time.monotonic() - started,
            findings=findings,
        )
    return findings, usage


def _openrouter_review_call(
    args: argparse.Namespace,
    pr: Mapping[str, object],
    *,
    model: str,
    key: str,
    diff: str,
    contents: dict[str, str],
    preload_coverage: Mapping[str, int] | None,
    note: str,
    label: str = "",
) -> tuple[list[dict], dict]:
    """One llm-engine review of ``diff`` — the whole PR, or one shard of it.

    The single place the review mode (single pass / consensus / fan-out /
    until-dry) is turned into model calls, so a shard is reviewed with exactly
    the prompts and consensus semantics an unsharded PR gets.
    """
    pr_owner, pr_repo_name = args.repo.split("/", 1)
    reasoning_effort = (
        None if args.reasoning_effort == "none" else args.reasoning_effort
    )
    # Consensus routes through fan-out because that is where the threading
    # already lives — repeats are extra threads in the same wave, never a
    # second round-trip. Without --fanout the repeated "aspect" is the plain
    # unnarrowed prompt (empty instruction), so consensus and aspect fan-out
    # compose instead of competing.
    n_passes = max(1, int(args.passes))
    until_dry = bool(getattr(args, "until_dry", False))
    until_dry_max = max(1, int(getattr(args, "until_dry_max", UNTIL_DRY_MAX_PASSES)))
    target = ReviewTarget(
        repo=args.repo,
        diff=diff,
        number=args.number,
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        file_contents={} if args.no_preload else contents,
        extra_context=note or None,
    )
    config = ReviewConfig(
        passes=n_passes,
        min_agreement=max(1, int(args.min_agreement)),
        until_dry=until_dry,
        until_dry_max=until_dry_max,
        route=ReviewRoute(
            name="review",
            # review() requires an OpenRouter-prefixed route model and strips it
            # before the transport call, so both prefixed and bare ids work.
            primary=model if model.startswith("openrouter/") else f"openrouter/{model}",
            fallback=model,
            data_policy="",
            cost_tier="",
        ),
    )
    if args.fanout or n_passes > 1 or until_dry:
        # `select_aspects` returns the always-on lenses plus any conditional
        # one whose evidence is present in THIS diff (e.g. `ui_style` only when
        # the diff touches front-end files). Jobs are one per (pass, aspect),
        # so gating on evidence is what keeps the lens catalogue from costing a
        # request per lens on every review — and stops a UI reviewer guessing
        # at a Python-only diff.
        aspects = (
            select_aspects(diff, repo=args.repo) if args.fanout else {"general": ""}
        )
        mode = (
            f"until-dry (max {until_dry_max} waves, stop on zero new groups)"
            if until_dry
            else (
                f"{n_passes} consensus pass(es) = "
                f"{len(aspects) * n_passes} parallel calls; "
                f"reporting findings seen by >= {args.min_agreement}"
            )
        )
        print(
            f"{label}fan-out: {len(aspects)} aspect pass(es) "
            f"({', '.join(aspects)}) x {mode}"
        )
    else:
        print(f"{label}prompt: {len(target.build_prompt())} chars")
    result = _io.review(
        target,
        config,
        api_key=key,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        structured=not args.no_structured,
        reasoning_effort=reasoning_effort,
        fanout=args.fanout,
        usage_context={
            "consumer": "sweep",
            "repo_pr": f"{pr_owner}/{pr_repo_name}#{args.number}",
            "mode": "review",
            **(preload_coverage or {}),
        },
        deadline=getattr(args, "deadline", None),
    )
    # The engine partitions earlier than this caller's render step; recombine so
    # summary-only advisories still reach render()/the artifact.
    return [*result.findings, *result.summary_only_findings], result.usage


def _run_shards(
    plan: Sequence[Shard], review_one: Callable[[Shard], tuple[list[dict], dict]]
) -> list[tuple[list[dict], dict]]:
    """Run ``review_one`` over the shards, ``SHARD_CONCURRENCY`` at a time.

    Fail closed and fast: the first shard that fails fails the whole review (a
    PR reviewed in three of four shards must not post a score for all four),
    and shards that have not started yet are cancelled so no more is spent.
    The failure keeps its original type so the caller's handling — and the
    sweep's quota/provider attribution, which parses the message — still work.
    """
    results: dict[int, tuple[list[dict], dict]] = {}
    with ThreadPoolExecutor(max_workers=SHARD_CONCURRENCY) as pool:
        futures = {
            pool.submit(copy_context().run, review_one, shard): shard for shard in plan
        }
        for fut in as_completed(futures):
            shard = futures[fut]
            try:
                results[shard.index] = fut.result()
            except ReviewError as exc:
                for other in futures:
                    other.cancel()
                raise ReviewError(
                    f"shard {shard.index}/{len(plan)} failed: {exc}"
                ) from exc
            except BaseException:
                for other in futures:
                    other.cancel()
                raise
    return [results[shard.index] for shard in plan]


def _drop_unverified_absence_in_shard(
    findings: list[dict], fully_preloaded: set[str], label: str
) -> list[dict]:
    """The absence guard, applied against what THIS shard actually read.

    Run again later against the union of every shard's preload, that guard
    would let a shard's "X is never called" pass on files only another shard
    had in front of it.
    """
    kept: list[dict]
    kept, dropped = _io.drop_unverified_absence_findings(findings, fully_preloaded)
    for finding in dropped:
        print(
            f"  {label}dropped (unverified absence claim; incomplete files: "
            f"{', '.join(finding.get('preload_missing_files') or [])}): "
            f"{str(finding.get('title') or finding.get('file'))[:60]}",
            file=sys.stderr,
        )
    return kept


def run_sharded_llm(
    args: argparse.Namespace,
    pr: Mapping[str, object],
    plan: Sequence[Shard],
    *,
    reviewable_diff: str,
    model: str,
    key: str,
    base_note: str,
) -> tuple[list[dict], dict, dict[str, str], set[str], dict[str, int]]:
    """Review each shard with :func:`llm_review_call` and merge the results.

    Returns ``(findings, usage, contents, fully_preloaded, preload_coverage)``
    shaped exactly like the unsharded path's, so every filter, the renderer
    and the marker downstream run unchanged.
    """
    n = len(plan)
    # Preloads are GitHub calls: run them sequentially, up front, so the
    # concurrent part below is model calls only.
    preloads: dict[int, tuple[dict[str, str], set[str], dict[str, int]]] = {}
    for shard in plan:
        preloads[shard.index] = _io.preload_files(
            args, pr, rank_changed_paths_by_hunks(shard.diff)
        )
    contents: dict[str, str] = {}
    fully_preloaded: set[str] = set()
    for c, fully, _cov in preloads.values():
        contents.update(c)
        fully_preloaded |= fully
    all_paths = rank_changed_paths_by_hunks(reviewable_diff)
    read = len(fully_preloaded & set(all_paths))
    preload_coverage = {
        "files_in_diff": len(all_paths),
        "files_preloaded": read,
        "files_truncated": len(all_paths) - read,
    }
    print(
        f"preloaded {read} of {len(all_paths)} file(s) across {n} shard(s); "
        f"{len(all_paths) - read} not fully read"
    )
    note = base_note
    submodule_ctx = _io.fetch_submodule_context(
        contents,
        reviewable_diff,
        args.repo,
        head_sha=str(pr.get("headRefOid") or ""),
        mention_text=f"{pr.get('title') or ''}\n{pr.get('body') or ''}",
    )
    if submodule_ctx:
        print("submodule: fetched cross-submodule import context")
        note = (note + "\n\n" + submodule_ctx).strip() if note else submodule_ctx

    def review_one(shard: Shard) -> tuple[list[dict], dict]:
        label = f"[shard {shard.index}/{n}] "
        lifecycle.stage("shard", shard=shard.index)
        shard_contents, shard_fully, shard_cov = preloads[shard.index]
        shard_note = "\n\n".join(
            part for part in (note, shard_context_note(shard, plan)) if part
        )
        started = time.monotonic()
        findings, usage = _io.llm_review_call(
            args,
            pr,
            model=model,
            key=key,
            diff=shard.diff,
            contents=shard_contents,
            preload_coverage=shard_cov,
            note=shard_note,
            label=label,
        )
        print(
            f"{label}{len(findings)} finding(s) in {time.monotonic() - started:.0f}s, "
            f"cost=${(usage.get('cost') or 0):.4f}"
        )
        return _io._drop_unverified_absence_in_shard(
            findings, shard_fully, label
        ), usage

    results = _io._run_shards(plan, review_one)
    findings = merge_shard_findings([r[0] for r in results])
    usage = merge_shard_usage([r[1] for r in results], plan)
    return findings, usage, contents, fully_preloaded, preload_coverage
