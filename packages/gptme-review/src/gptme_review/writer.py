"""Per-PR review writer: marker comment and publish helpers.

Writer-port slice 2a (Bob's knowledge/technical-designs/2026-11-20-ai-review-writer-port.md).
Code moved as-is from Bob's ``scripts/github/ai-review.py``; review behaviour is
unchanged.

**I/O and identity resolve at call time through** ``_io``. A host calls
:func:`bind_io` with a namespace providing ``MARKER_NAMESPACE``,
``MARKER_PREFIX``, ``PROVISIONAL_MARKER_PREFIX``, ``REVIEWER_IDENTITY`` (an
object with ``login``) and the I/O seams (``run``,
``gh_paginated_list``; ``_find_all_marker_comments`` / ``_delete_marker_comment``
optionally override the writer's own) and uses the functions it returns. Bob
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
import re
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Protocol

from bobutils.shell import gh_paginated_list

from gptme_review.core import (
    ReviewDeadline,
    is_summary_only_finding,
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
