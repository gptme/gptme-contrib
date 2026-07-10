"""PM worker post-session bookkeeping (record writing, PR-state diff, delivery check).

Behavior-identical port of the inline ``python3`` heredoc blocks from
ErikBjare/bob ``scripts/github/project-monitoring-worker.sh`` (step 3 of the
Phase-2 execution-consolidation migration; see
``knowledge/technical-designs/reactive-dispatch-phase2-3-execution-consolidation.md``
in that repo). Steps 1-2 were contrib#1261 (:mod:`gptme_runloops.merge_lifecycle`)
and contrib#1262 (:mod:`gptme_runloops.prompt_templates`). The bash remains the
runtime hotpath until the brain-side shim PR; the golden/table tests in
``tests/test_worker_records.py`` are what that shim will diff against.

Bash source blocks ported (ErikBjare/bob @
98e7da0eb18543c53ca7f7ee207eb679010ccffb):

- ``project-monitoring-worker.sh:110-127`` — CC rate-limit log parser
  (``python3 - "$_cc_log" <<'PYEOF'``): find the first *rejected*
  ``rate_limit_event`` in a stream-json log → :func:`parse_rate_limit_rejection`.
- ``project-monitoring-worker.sh:283-323`` — primary session-record write via
  ``gptme_sessions.post_session`` → :func:`write_post_session_record`
  (``post_session``/``make_store`` injected by the caller).
- ``project-monitoring-worker.sh:325-373`` — legacy fallback record write via
  ``metaproductivity.sessions.SessionRecord`` → :func:`fallback_outcome` +
  :func:`write_fallback_session_record` (``make_record`` injected).
- ``project-monitoring-worker.sh:377-502`` — PR-state before/after diff +
  deliverables dedupe + outcome upgrade hook → :func:`parse_pr_snapshot`,
  :func:`apply_pr_state_diff`, :func:`update_record_pr_state`
  (``fetch_pr_snapshot`` is the default gh adapter; ``upgrade_outcome``
  injected).
- ``project-monitoring-worker.sh:505-627`` — worker-result manifest build +
  record enrichment → :func:`collect_commit_oids`,
  :func:`apply_worker_result_to_payload`, :func:`write_worker_result_manifest`
  (``agent_events.worker_results`` functions injected).
- ``project-monitoring-worker.sh:651-666`` — delivery-check JSON field
  extractor (``python3 -c``) → :func:`extract_delivery_field`; plus the
  surrounding pure bash decisions ``project-monitoring-worker.sh:668-690``
  → :func:`normalize_delivery_outcome`, :func:`compute_latency_outcome`.
- ``project-monitoring-worker.sh:692-718`` — latency-ledger append via
  ``metaproductivity.project_monitoring_latency`` →
  :func:`append_worker_latency_records` (``append_latency_records`` injected).
- ``project-monitoring-worker.sh:743-766`` — auto wait-and-merge gate JSONL
  log entry → :func:`build_wait_merge_gate_entry`,
  :func:`append_wait_merge_gate_log`.
- ``project-monitoring-worker.sh:70`` — the ``_item_types_json`` splitter
  (heredoc-injection hardening) → :func:`split_item_types`.
- ``project-monitoring-worker.sh:808-815`` — arc auto-close PR-state reader
  (``python3 -c``) → :func:`read_record_pr_state_after`.

Design rules (same as steps 1-2):

- Parse/compute/diff logic is pure: data in, data out. File and subprocess
  I/O is separated into thin orchestrators whose side-effecting collaborators
  (``post_session``, ``make_record``, ``build_worker_result``,
  ``append_latency_records``, the gh snapshot fetcher) are injected — the
  agent-side packages they come from (``gptme_sessions``, brain-side
  ``metaproductivity``/``agent_events``) are NOT imported here.
- Bob-specific paths/config are parameters with defaults; nothing points at
  ``/home/bob``.
- The bash heredocs receive values via ``${var@Q}`` string interpolation
  (which is why the bash pre-encodes ``item_types`` as JSON — worker.sh:67-70);
  these functions take real parameters, eliminating that injection class.
  The brain shim passes values via argv/env instead.
- Failure contract: the bash runs each heredoc under ``|| true`` (or uses a
  non-zero exit to trigger the legacy fallback), so a heredoc that dies
  mid-way simply skips that bookkeeping step. The orchestrators preserve
  this by *raising* on the same conditions the heredocs die on (bad JSON in
  required inputs, gh timeout, non-numeric PR number); callers wrap with the
  same tolerance the bash uses.
- Where the bash behavior is quirky, the port preserves it and marks the
  spot with a ``# NOTE(parity):`` comment. Behavior changes come later, with
  the brain-side switchover.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Commit-oid extraction from deliverable strings (worker.sh:519).
# NOTE(parity): re.IGNORECASE is redundant in the bash heredoc too — findall
# runs on ``deliverable.lower()`` — but it is part of the source pattern, so
# it is preserved.
COMMIT_OID_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)

# Latency outcomes the bash case-whitelist accepts (worker.sh:671-674).
KNOWN_DELIVERY_OUTCOMES: frozenset[str] = frozenset(
    {"handled", "orphan_no_delivery", "no_action_needed", "failed"}
)


# --- CC rate-limit log parsing (worker.sh:110-127) ---


@dataclass(frozen=True)
class RateLimitRejection:
    """The first *rejected* rate_limit_event found in a CC stream-json log.

    Field values are whatever JSON types the log carried (``resetsAt`` is
    typically an int epoch); :meth:`render` stringifies them exactly the way
    the heredoc's ``print(..., sep='\\t')`` does.
    """

    rate_limit_type: Any
    resets_at: Any

    def render(self) -> str:
        """The heredoc's stdout line, consumed by the bash via ``cut -f1/-f2``."""
        return f"{self.rate_limit_type}\t{self.resets_at}"


def parse_rate_limit_rejection(lines: Iterable[str]) -> RateLimitRejection | None:
    """Find the first REJECTED rate_limit_event in a stream-json log.

    Mirrors worker.sh:110-127: blank lines and JSON-invalid lines are
    skipped; the first ``type == "rate_limit_event"`` whose
    ``rate_limit_info.status == "rejected"`` wins; missing info keys yield
    ``""``. Returns ``None`` when no rejection is present (the heredoc
    prints nothing → the bash sees ``_cc_rl_info=""`` and does NOT block).

    NOTE(parity): a rate_limit_event whose ``rate_limit_info`` is not a dict
    raises (the heredoc dies on ``.get`` of a non-dict), aborting the scan
    even if a later line holds a valid rejection. The bash catches the
    non-zero exit (``|| _cc_rl_info=""``) and treats it as "no rejection".
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # NOTE(parity): non-dict JSON lines (arrays, strings) die on .get in
        # the heredoc; same here.
        if d.get("type") == "rate_limit_event":
            info = d.get("rate_limit_info", {})
            if info.get("status") == "rejected":
                return RateLimitRejection(
                    rate_limit_type=info.get("rateLimitType", ""),
                    resets_at=info.get("resetsAt", ""),
                )
    return None


# --- Shared helpers ---


def resolve_trajectory(path_str: str | None) -> Path | None:
    """Trajectory-path resolution shared by both record writers (worker.sh:301-303).

    Empty/None → ``None``; a path that is not an existing regular file →
    ``None``.
    """
    trajectory = Path(path_str) if path_str else None
    if trajectory is not None and not trajectory.is_file():
        return None
    return trajectory


def split_item_types(text: str) -> list[str]:
    """Whitespace-split an ``item_types`` string (worker.sh:70).

    The bash pre-encodes this as a JSON array purely to survive ``${var@Q}``
    heredoc injection; with real parameters the split is all that remains.
    """
    return text.split()


def _write_record(record_path: Path, payload: Mapping[str, Any]) -> None:
    """Serialize a record the way every heredoc does (``ensure_ascii=False``)."""
    record_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --- Primary session-record write (worker.sh:283-323) ---


def finalize_post_session_record(
    record: Mapping[str, Any],
    grade: Any,
    item_timeout: int,
) -> dict[str, Any]:
    """Post-process a ``post_session`` record dict (worker.sh:318-321).

    Adds the grade and, when a positive timeout was configured, the
    ``timeout_seconds`` field.
    """
    finalized = dict(record)
    finalized["grade"] = grade
    if item_timeout > 0:
        finalized["timeout_seconds"] = item_timeout
    return finalized


def write_post_session_record(
    record_file: Path | str,
    *,
    harness: str,
    model: str | None,
    session_id: str,
    exit_code: int,
    duration_seconds: int,
    item_timeout: int,
    trajectory_path: str | None,
    post_session: Callable[..., Any],
    make_store: Callable[[Path], Any],
) -> None:
    """Write the monitoring session record via an injected ``post_session``.

    Mirrors worker.sh:283-323. ``post_session`` and ``make_store`` are the
    caller's ``gptme_sessions.post_session.post_session`` and a
    ``SessionStore`` factory taking the sessions dir (the record file's
    parent) — injected so this package does not depend on gptme-sessions.

    Raises on any failure (the bash uses the heredoc's non-zero exit to
    trigger the legacy fallback writer).
    """
    record_path = Path(record_file)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory = resolve_trajectory(trajectory_path)

    store = make_store(record_path.parent)
    result = post_session(
        store=store,
        harness=harness,
        # NOTE(parity): empty model → None here, but → "unknown" in the
        # fallback writer below (worker.sh:309 vs :358). Preserved.
        model=model if model else None,
        run_type="monitoring",
        trigger="timer",
        category="pm-react",
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        trajectory_path=trajectory,
        session_id=session_id,
    )
    record = finalize_post_session_record(
        result.record.to_dict(), result.grade, item_timeout
    )
    _write_record(record_path, record)


# --- Legacy fallback record write (worker.sh:325-373) ---


def fallback_outcome(exit_code: int) -> str:
    """The fallback writer's outcome heuristic (worker.sh:325-326).

    NOTE(parity): a timeout (exit 124) records as ``"unknown"``, not
    ``"failed"`` or ``"timeout"`` — only non-zero non-124 exits are
    ``"failed"``. Preserved.
    """
    if exit_code != 0 and exit_code != 124:
        return "failed"
    return "unknown"


def build_fallback_record_payload(
    base_record: Mapping[str, Any],
    *,
    session_id: str,
    duration_seconds: int,
    exit_code: int,
    item_timeout: int,
    trajectory: Path | None,
) -> dict[str, Any]:
    """Augment a base SessionRecord dict the way worker.sh:364-372 does.

    NOTE(parity): ``duration_seconds`` is re-assigned even though the base
    record already carries it (worker.sh:366 duplicates :363's constructor
    argument). Preserved.
    """
    payload = dict(base_record)
    payload["session_id"] = session_id
    payload["duration_seconds"] = duration_seconds
    payload["exit_code"] = int(exit_code)
    if item_timeout > 0:
        payload["timeout_seconds"] = item_timeout
    if trajectory is not None:
        payload["trajectory_path"] = str(trajectory)
    return payload


def write_fallback_session_record(
    record_file: Path | str,
    *,
    harness: str,
    model: str | None,
    outcome: str,
    session_id: str,
    exit_code: int,
    duration_seconds: int,
    item_timeout: int,
    trajectory_path: str | None,
    make_record: Callable[..., Mapping[str, Any]],
) -> None:
    """Write the legacy fallback record (worker.sh:335-373).

    ``make_record`` is the caller's base-record factory — e.g.
    ``lambda **kw: SessionRecord(**kw).to_dict()`` over the brain's
    ``metaproductivity.sessions.SessionRecord`` — injected so this package
    does not depend on brain-side code. It receives exactly the constructor
    arguments the heredoc passes (worker.sh:356-363).
    """
    record_path = Path(record_file)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory = resolve_trajectory(trajectory_path)
    base_record = make_record(
        harness=harness,
        model=model if model else "unknown",
        run_type="monitoring",
        category="pm-react",
        outcome=outcome,
        duration_seconds=duration_seconds,
    )
    payload = build_fallback_record_payload(
        base_record,
        session_id=session_id,
        duration_seconds=duration_seconds,
        exit_code=exit_code,
        item_timeout=item_timeout,
        trajectory=trajectory,
    )
    _write_record(record_path, payload)


# --- PR-state before/after diff (worker.sh:377-502) ---


def normalize_oid(value: Any) -> str:
    """worker.sh:385-386 — stringified, stripped, lowercased ('' for falsy)."""
    return str(value or "").strip().lower()


def normalize_pr_snapshot(payload: Any) -> dict[str, str]:
    """Normalize a ``gh pr view --json state,headRefOid,mergeCommit`` payload.

    Mirrors worker.sh:389-399: non-dict payloads → ``{}``; ``mergeCommit``
    may be the gh object form (``{"oid": ...}``) or a bare value; state is
    uppercased, oids lowercased.
    """
    if not isinstance(payload, dict):
        return {}
    merge_commit = payload.get("mergeCommit")
    if isinstance(merge_commit, dict):
        merge_commit = merge_commit.get("oid")
    return {
        "state": str(payload.get("state") or "").strip().upper(),
        "headRefOid": normalize_oid(payload.get("headRefOid")),
        "mergeCommit": normalize_oid(merge_commit),
    }


def parse_pr_snapshot(raw: str) -> dict[str, str]:
    """Parse a raw snapshot JSON string; empty/invalid → ``{}`` (worker.sh:402-410)."""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return normalize_pr_snapshot(payload)


def dedupe_deliverables(values: Sequence[Any]) -> list[str]:
    """Case-insensitive dedupe keeping the first spelling (worker.sh:436-448).

    NOTE(parity): this differs from the worker-result manifest's dedupe
    (:func:`normalize_manifest_deliverables`), which is case-SENSITIVE.
    Both are preserved as-is.
    """
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def apply_pr_state_diff(
    payload: dict[str, Any],
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> dict[str, Any]:
    """Fold normalized before/after snapshots into a record payload.

    Pure core of worker.sh:459-486: sets ``pr_state_before/after``,
    ``pr_head_oid_before/after``, ``pr_merge_commit_before/after`` (each
    only when truthy — an empty observation never clears a field), appends
    the new head to deliverables when the head advanced, and replaces
    ``deliverables`` with the case-insensitively deduped list (always, even
    when empty). Mutates and returns ``payload``.
    """
    deliverables = payload.get("deliverables")
    if not isinstance(deliverables, list):
        deliverables = []

    before_head = before.get("headRefOid", "")
    after_head = after.get("headRefOid", "")

    if before.get("state"):
        payload["pr_state_before"] = before["state"]
    if before_head:
        payload["pr_head_oid_before"] = before_head
    if before.get("mergeCommit"):
        payload["pr_merge_commit_before"] = before["mergeCommit"]

    if after.get("state"):
        payload["pr_state_after"] = after["state"]
    if after_head:
        payload["pr_head_oid_after"] = after_head
    if after.get("mergeCommit"):
        payload["pr_merge_commit_after"] = after["mergeCommit"]

    if before_head and after_head and before_head != after_head:
        deliverables.append(after_head)

    payload["deliverables"] = dedupe_deliverables(deliverables)
    return payload


def fetch_pr_snapshot(
    repo: str,
    number: int | str,
    *,
    cwd: str | Path,
    timeout: float = 20,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Fetch the live PR snapshot via gh (worker.sh:413-433).

    A non-zero gh exit → ``{}``; unparseable stdout → ``{}``.

    NOTE(parity): a gh *timeout* raises (``subprocess.TimeoutExpired``),
    killing the whole diff step — including the already-parsed before-fields
    — exactly as the heredoc dies under its ``|| true``. Preserved.
    """
    result = runner(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "state,headRefOid,mergeCommit",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        return {}
    return parse_pr_snapshot(result.stdout)


def update_record_pr_state(
    record_file: Path | str,
    *,
    repo: str,
    number: int | str,
    before_json: str,
    cwd: str | Path,
    fetch: Callable[[str, int], dict[str, str]] | None = None,
    upgrade_outcome: Callable[[dict[str, Any]], Any] | None = None,
) -> None:
    """Run the whole PR-state diff step against a record file (worker.sh:377-502).

    ``fetch`` overrides the default gh adapter (tests); ``upgrade_outcome``
    is the caller's ``metaproductivity.pr_outcome.upgrade_outcome_from_pr_state``
    or ``None`` when unavailable — the heredoc's ``except ImportError: pass``
    (worker.sh:495-499) maps to passing ``None``.

    Missing or non-dict record → silently returns (worker.sh:451-457).
    """
    record_path = Path(record_file)
    if not record_path.is_file():
        return

    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return

    before = parse_pr_snapshot(before_json)
    pr_number = int(number)
    if fetch is not None:
        after = fetch(repo, pr_number)
    else:
        after = fetch_pr_snapshot(repo, pr_number, cwd=cwd)

    apply_pr_state_diff(payload, before, after)

    if upgrade_outcome is not None:
        upgrade_outcome(payload)

    _write_record(record_path, payload)


# --- Worker-result manifest (worker.sh:505-627) ---


def normalize_head(value: Any) -> str | None:
    """worker.sh:522-524 — stripped+lowercased str, ``None`` when empty."""
    text = str(value or "").strip().lower()
    return text or None


def normalize_manifest_deliverables(raw: Any) -> list[str]:
    """Case-SENSITIVE deliverables dedupe (worker.sh:527-538).

    NOTE(parity): unlike :func:`dedupe_deliverables` (the PR-state diff's
    case-insensitive dedupe), the manifest builder keys on the stripped text
    itself, so ``"abc"`` and ``"ABC"`` both survive here. Preserved.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def collect_commit_oids(
    deliverables: Sequence[str],
    before_head: str | None,
    after_head: str | None,
) -> list[str]:
    """Extract commit oids from deliverables + the head advance (worker.sh:541-554).

    Hex tokens (7-40 chars) are pulled from each lowercased deliverable in
    order, deduped; a head advance appends the new head if not already seen.
    """
    commit_oids: list[str] = []
    for deliverable in deliverables:
        for match in COMMIT_OID_RE.findall(deliverable.lower()):
            if match not in commit_oids:
                commit_oids.append(match)
    if (
        before_head
        and after_head
        and before_head != after_head
        and after_head not in commit_oids
    ):
        commit_oids.append(after_head)
    return commit_oids


def apply_worker_result_to_payload(
    payload: dict[str, Any],
    normalized: Mapping[str, Any],
    manifest_path: Path | str,
) -> dict[str, Any]:
    """Reflect the written manifest back into the record payload.

    Pure core of worker.sh:604-626: worker_result_path, worker_status
    (``"failed"`` when the manifest carries no status), the schema tag,
    optional blocked-reason, artifact paths (truthy values only, stringified),
    git refs, and the intended category. Mutates and returns ``payload``.
    """
    payload["worker_result_path"] = str(manifest_path)
    payload["worker_status"] = str(normalized.get("status") or "failed")
    payload["worker_manifest_schema"] = (
        f"bob.worker-result.v{normalized.get('schema_version', 1)}"
    )

    blocked_reason = normalized.get("blocked_reason")
    if blocked_reason:
        payload["worker_blocked_reason"] = str(blocked_reason)

    artifact_paths = normalized.get("artifact_paths")
    if isinstance(artifact_paths, dict):
        payload["worker_artifact_paths"] = {
            key: str(value) for key, value in artifact_paths.items() if value
        }

    git_refs = normalized.get("git_refs")
    if isinstance(git_refs, dict):
        payload["worker_git_refs"] = git_refs

    task = normalized.get("task")
    if isinstance(task, dict) and task.get("intended_category"):
        payload["worker_intended_category"] = str(task["intended_category"])

    return payload


def write_worker_result_manifest(
    record_file: Path | str,
    *,
    repo: str,
    number: int | str,
    session_id: str,
    exit_code: int,
    duration_seconds: int,
    model: str | None,
    item_types: Sequence[str],
    build_worker_result: Callable[..., dict[str, Any]],
    write_worker_result: Callable[[Path, Mapping[str, Any]], Any],
    load_worker_result: Callable[[Path], Mapping[str, Any] | None],
) -> None:
    """Build + write the worker-result manifest and enrich the record.

    Mirrors worker.sh:505-627. The three ``agent_events.worker_results``
    functions are injected (they live in the caller's workspace packages,
    not here). Missing or non-dict record → silently returns.

    ``build_worker_result`` must return a manifest that already contains the
    ``git_refs``, ``task``, and ``artifact_paths`` submaps (the real
    ``agent_events.build_worker_result`` always does).

    NOTE(parity): ``int(number)`` raises on an empty/non-numeric PR number
    (worker.sh:565), killing the manifest step under the bash ``|| true`` —
    the record then simply never gains ``worker_result_path``. Preserved.

    NOTE(parity): the submap enrichment below hard-indexes ``git_refs``/
    ``task``/``artifact_paths`` exactly like worker.sh:595-600 — a builder
    that omits them raises before the manifest is written, and the record
    never gains ``worker_result_path``, same as the heredoc dying under its
    ``|| true``. Softening this (setdefault) would silently accept manifests
    the bash rejects; a behavior change for the switchover to consider.
    """
    record_path = Path(record_file)
    if not record_path.is_file():
        return

    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return

    pr_number = int(number)
    deliverables = normalize_manifest_deliverables(payload.get("deliverables"))
    before_head = normalize_head(payload.get("pr_head_oid_before"))
    after_head = normalize_head(payload.get("pr_head_oid_after"))
    manifest_path = record_path.with_name(f"{record_path.stem}.worker-result.json")
    manifest = build_worker_result(
        worker_kind="project-monitoring-worker",
        worker_id=f"project-monitoring-{session_id}",
        work_item_id=f"{repo}#{pr_number}",
        session_id=session_id,
        draft_path=record_path.with_name(
            f"{record_path.stem}.worker-result.draft.json"
        ),
        exit_code=int(exit_code),
        duration_seconds=int(duration_seconds),
        started_at=None,
        ended_at=None,
        model=model if model else None,
        intended_category="pm-react",
        repo=repo,
        issue_number=pr_number,
        title=None,
        attempt=None,
        deliverables=deliverables,
        start_commit=before_head,
        end_commit=after_head,
        branch=None,
        output_file=None,
        trajectory_path=str(payload.get("trajectory_path") or "") or None,
        worktree_dir=None,
    )
    manifest["git_refs"]["commit_oids"] = collect_commit_oids(
        deliverables, before_head, after_head
    )
    manifest["task"]["item_types"] = list(item_types)
    manifest["task"]["run_type"] = str(payload.get("run_type") or "monitoring")
    manifest["task"]["recorded_outcome"] = str(payload.get("outcome") or "")
    manifest["artifact_paths"]["record_path"] = str(record_path)
    manifest["artifact_paths"].pop("draft_path", None)
    write_worker_result(manifest_path, manifest)
    normalized = load_worker_result(manifest_path) or manifest

    apply_worker_result_to_payload(payload, normalized, manifest_path)
    _write_record(record_path, payload)


# --- Delivery check (worker.sh:651-666 extractor; :668-690 decisions) ---


def extract_delivery_field(raw_json: str, field: str) -> str:
    """The delivery-result field extractor (``python3 -c``, worker.sh:651-666).

    Any parse failure → empty payload; booleans render as ``"true"``/
    ``"false"``, ``None``/missing as ``""``, everything else via ``str``.
    """
    try:
        payload = json.loads(raw_json)
    except Exception:
        payload = {}
    # NOTE(parity): a JSON scalar (e.g. `"handled"` or `5`) parses fine but
    # dies on .get in the -c snippet; the bash captures the empty stdout of
    # the failed command, so the observable result is "". Short-circuit
    # non-dict payloads to {} for the same observable "".
    if not isinstance(payload, dict):
        payload = {}
    value = payload.get(field)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def normalize_delivery_outcome(raw: str) -> str:
    """Whitespace-strip + whitelist the delivery outcome (worker.sh:668-674).

    NOTE(parity): the bash ``tr -d '[:space:]'`` deletes ALL whitespace,
    including interior (``"no action"`` → ``"noaction"`` → unknown →
    ``"handled"``). Unknown outcomes normalize to ``"handled"``. The
    character class is ASCII-only (tr's ``[:space:]``), not Python's
    unicode-aware ``\\s``.
    """
    cleaned = re.sub(r"[ \t\n\r\f\v]+", "", raw)
    if cleaned in KNOWN_DELIVERY_OUTCOMES:
        return cleaned
    return "handled"


def compute_latency_outcome(
    delivery_outcome: str,
    exit_code: int,
    *,
    needs_fallback_reply: str,
    fallback_reply_posted: str,
) -> str:
    """The latency-ledger outcome decision (worker.sh:683-690).

    The two flags arrive as the extractor's rendered strings (``"true"``,
    ``"false"``, or ``""``) and are compared literally, as the bash does.
    A failed session whose missing reply could not be backfilled records
    ``orphan_no_delivery`` (only when the delivery check *asked* for a
    fallback); any other non-zero-exit case records ``failed``; otherwise
    the delivery outcome passes through.
    """
    if exit_code != 0 and fallback_reply_posted != "true":
        if delivery_outcome == "orphan_no_delivery" and needs_fallback_reply == "true":
            return "orphan_no_delivery"
        return "failed"
    return delivery_outcome


# --- Latency-ledger append (worker.sh:692-718) ---


def parse_latency_inputs(
    item_json: str,
    latency_context_json: str,
    ack_result_json: str,
) -> tuple[Any, Any, Any]:
    """Decode the three JSON inputs the way the heredoc does (worker.sh:707-716).

    Empty strings select the defaults (``[]`` for the context, ``None`` for
    the ack result); invalid JSON raises, killing the append under the bash
    ``|| true``. ``item_json`` has no empty-string arm in the heredoc —
    ``json.loads("")`` raises. Preserved.
    """
    item = json.loads(item_json)
    latency_context = json.loads(latency_context_json) if latency_context_json else []
    ack_result = json.loads(ack_result_json) if ack_result_json else None
    return item, latency_context, ack_result


def append_worker_latency_records(
    *,
    repo_root: Path | str,
    item_json: str,
    latency_context_json: str,
    ack_result_json: str,
    handled_at: str,
    session_id: str,
    outcome: str,
    append_latency_records: Callable[..., Any],
) -> None:
    """Append this pass to the latency ledger via an injected recorder.

    Mirrors worker.sh:692-718; ``append_latency_records`` is the caller's
    ``metaproductivity.project_monitoring_latency.append_latency_records``.
    """
    item, latency_context, ack_result = parse_latency_inputs(
        item_json, latency_context_json, ack_result_json
    )
    append_latency_records(
        repo_root=Path(repo_root),
        item=item,
        latency_context=latency_context,
        handled_at=handled_at,
        session_id=session_id,
        outcome=outcome,
        ack_result=ack_result,
    )


# --- Auto wait-and-merge gate log (worker.sh:743-766) ---


def build_wait_merge_gate_entry(
    gate_json: str,
    *,
    timestamp: str,
    repo: str,
    pr_number: int | str,
    item_types: Sequence[str],
    session_id: str,
    session_start: str,
    gate_exit_code: int,
) -> dict[str, Any]:
    """Build one gates.jsonl entry from the gate script's JSON verdict.

    Mirrors worker.sh:748-761: an empty ``gate_json`` → ``{}``; a non-dict
    payload → ``{}``; invalid JSON raises (killed by the bash ``|| true`` —
    no log line). The fixed bookkeeping fields overwrite any same-named
    fields the gate emitted.
    """
    payload = json.loads(gate_json) if gate_json else {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(
        {
            "timestamp": timestamp,
            "repo": repo,
            "pr_number": int(pr_number),
            "item_types": list(item_types),
            "session_id": session_id,
            "session_start": session_start,
            "gate_exit_code": int(gate_exit_code),
        }
    )
    return payload


def append_wait_merge_gate_log(
    log_path: Path | str,
    entry: Mapping[str, Any],
) -> None:
    """Append a gate entry as a sorted-keys JSONL line (worker.sh:762-765)."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


# --- Arc auto-close PR-state reader (worker.sh:808-815) ---


def read_record_pr_state_after(record_path: Path | str) -> str:
    """Read ``pr_state_after`` (uppercased) from a record file, ``""`` on any error.

    Mirrors the ``python3 -c`` snippet at worker.sh:808-815, whose bare
    ``except Exception: pass`` yields empty stdout for a missing file, bad
    JSON, or a non-dict payload.
    """
    try:
        with open(record_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return str(data.get("pr_state_after", "")).upper()
    except Exception:
        return ""
