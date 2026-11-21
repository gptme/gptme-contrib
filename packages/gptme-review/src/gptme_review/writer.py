"""Per-PR review writer: marker, publish, thread and disposition helpers.

Writer-port slices 2a and 2b (Bob's knowledge/technical-designs/2026-11-20-ai-review-writer-port.md).
Code moved as-is from Bob's ``scripts/github/ai-review.py``; review behaviour is
unchanged.

**I/O and identity resolve at call time through** ``_io``. A host calls
:func:`bind_io` with a namespace providing ``MARKER_NAMESPACE``,
``MARKER_PREFIX``, ``PROVISIONAL_MARKER_PREFIX``, ``REVIEWER_IDENTITY`` (an
object with ``login``) and the I/O seams (``run``, ``gh_json``,
``gh_paginated_list``; ``_find_all_marker_comments`` / ``_delete_marker_comment``
/ ``fetch_review_comments`` optionally override the writer's own) and uses the functions it returns. Bob
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
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Protocol

from bobutils.shell import gh_json, gh_paginated_list

from gptme_review.core import (
    ReviewDeadline,
    blocking_shortfall,
    filter_suppressed,
    is_summary_only_finding,
    matches_entry,
    normalize_finding_class,
    normalize_severity,
    suppression_entry,
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


def default_io(identity: SweepIdentity) -> SimpleNamespace:
    """A peer host's namespace: this module's I/O plus *identity*'s markers."""
    ns = identity.marker_prefix
    return SimpleNamespace(
        REVIEWER_IDENTITY=identity,
        run=run,
        gh_json=gh_json,
        gh_paginated_list=gh_paginated_list,
        MARKER_NAMESPACE=ns,
        MARKER_PREFIX=f"<!-- {ns}",
        PROVISIONAL_MARKER_PREFIX=f"<!-- {ns}-publishing",
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

    def route_override(self, args: argparse.Namespace) -> tuple[str, str] | None:
        """Return ``(route, why)`` to override an auto-resolved llm route, or None."""
        return None


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
