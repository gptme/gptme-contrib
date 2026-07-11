"""The ``run-item`` executor: run one PM work-file end-to-end (step 4).

Behavior-identical port of the ``PM_DETACHED=1`` slot path of ErikBjare/bob
``scripts/runs/github/project-monitoring.sh`` (lines 336-695) plus the sourced
``run_item_worker()`` (``project-monitoring-worker.sh``), composing the
step-1-3 modules into the uniform "run.sh-shaped" executor surface:

- **step 1** :mod:`gptme_runloops.merge_lifecycle` — self-merge gate +
  Greptile trigger/fix routing (``run_merge_lifecycle``);
- **step 2/4** :mod:`gptme_runloops.prompt_templates` — greptile fix
  instructions (``render_instruction``), per-type investigate arms
  (``build_investigate``), the main prompt skeleton (``render_main_prompt``);
- **step 3** :mod:`gptme_runloops.worker_records` — post-session bookkeeping
  (record write + fallback, PR-state diff, worker-result manifest, delivery
  fields, latency append, wait-merge gate log).

Interface design: ``knowledge/technical-designs/pm-run-item-executor-design.md``
(ErikBjare/bob). Bash source @ f95ccae920af0058c68c86083e9007be86710dc5.

Design rules (same as steps 1-3):

- **Plan/execute split** (§4.5): :func:`plan_item` is pure — everything
  decided before side effects (prompt bytes, timeout tier, session id,
  record/trajectory paths, claim key) — and :func:`run_work_file` /
  :func:`run_post_session` do the I/O through injected collaborators.
- **Policy injection** (§4.3): everything agent-specific (workspace path,
  identity strings, repo allowlists, helper paths, ledger paths) arrives via
  :class:`RunItemConfig` / :class:`RunItemHooks`; the CLI assembles defaults
  from workspace conventions and an optional TOML file. Nothing here points
  at ``/home/bob``.
- **run.sh stays the session runner** (§4.4): the native ``Executor``
  registry lacks codex/copilot and the trajectory-recovery machinery lives in
  run.sh; ``hooks.runner`` invokes it as a subprocess with the same argv/env
  contract. Native-executor adoption is an explicit later step.
- Where the bash behavior is quirky it is preserved and marked with
  ``# NOTE(parity):``. Brain-side concerns the step-5 switchover must wire
  (not half-implemented here) are marked ``# NOTE(step5):``.

# NOTE(step5): the step-5 slot-unit switchover must wire, around this
# executor, the pieces of the detached bash process that live OUTSIDE
# project-monitoring.sh:336-695 and are deliberately NOT absorbed here:
#
# 1. Pre-gates run at the top of project-monitoring.sh even when
#    PM_DETACHED=1: the maintenance-window gate, ``check_rate_limits``, the
#    memory-pressure gate (``_pm_check_memory_pressure``), the
#    auth-stale preflight, and ``ensure_graphql_attribution_wrapper``.
#    Either the dispatcher keeps running them pre-launch or the step-5
#    wrapper script runs them before exec'ing ``run-item``.
# 2. Backend incident guards (gptme memory-explosion, codex/copilot OOM,
#    gpt-5.4 NOOP) are re-applied INSIDE the detached bash process today;
#    ``run-item`` consumes ``--backend`` verbatim (routing stays out of the
#    executor), so until step 6 (incident block-files) the dispatcher must
#    enforce them before launch. Same for ``resolve-model.sh`` aliasing.
# 3. Post-run summary ``run_post_monitoring`` (run-type bandit update +
#    unified parent session record aggregating the per-item records) and the
#    cross-repo-supply-probe / resolver-outcome-recorder refreshes run after
#    the dispatch loop in the bash. ``hooks.post_run`` is the declared
#    subprocess seam for the summary (env contract documented on the field);
#    the brain needs a thin CLI wrapper over ``run_post_monitoring`` for it.
#    Without it, per-item records in a temp records dir are not aggregated
#    into the persistent session store (A/B cutover criterion 2).
# 4. Exit-code semantics: the bash detached path always exits 0 (worker
#    failures only feed the fail counter); per the interface design §4.1
#    ``run-item`` exits with the session's exit code instead, so the step-5
#    unit must add ``SuccessExitStatus=124`` (or accept unit-state changes)
#    before the vitals that read unit states compare A/B paths.
# 5. ``BOB_AMBIENT_HARNESS`` tagging: the brain config must set
#    ``ambient_harness_env = "BOB_AMBIENT_HARNESS"`` for ambient-memory
#    injection parity (the package default sets no env var).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gptme_runloops.merge_lifecycle import (
    LifecycleConfig,
    LifecycleResult,
    MergeLifecycleIO,
    SelfMergeCheckResult,
    WorkItem,
    run_merge_lifecycle,
)
from gptme_runloops.pm_dispatch import append_full_ledger_entry, is_direct_mention
from gptme_runloops.prompt_templates import (
    ItemPromptParams,
    build_investigate,
    render_arc_context,
    render_instruction,
    render_main_prompt,
    render_mention_constraint,
    render_preheld_claim_block,
)
from gptme_runloops.worker_records import (
    append_wait_merge_gate_log,
    append_worker_latency_records,
    build_wait_merge_gate_entry,
    compute_latency_outcome,
    extract_delivery_field,
    fallback_outcome,
    normalize_delivery_outcome,
    parse_rate_limit_rejection,
    read_record_pr_state_after,
    update_record_pr_state,
    write_fallback_session_record,
    write_post_session_record,
    write_worker_result_manifest,
)

# Item types that enable the PR-state diff / wait-merge / pr-before snapshot
# steps (worker.sh:72,379,555 — `grep -qwE "pr_update|merge_ready"`).
PR_STATE_TYPES: frozenset[str] = frozenset({"pr_update", "merge_ready"})

# CC stream-json trajectory floor (worker.sh:205) and grok floor (worker.sh:218).
CC_TRAJECTORY_MIN_BYTES = 5000
GROK_TRAJECTORY_MIN_BYTES = 1000

_DIGITS_RE = re.compile(r"[0-9]+")


logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    # Progress/diagnostic lines mirror the bash echoes. They go through
    # logging (the CLI configures a plain stderr handler) so `--dry-run`
    # stdout stays pure ExecutionPlan JSON (the §5.1 plan-diff surface);
    # under a systemd slot unit both streams land in the journal.
    logger.info(msg)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_iso_seconds(dt: datetime) -> str:
    """``date --iso-8601=seconds`` equivalent (local time with UTC offset)."""
    return dt.astimezone().isoformat(timespec="seconds")


# --- Item schema (design §4.2) ---


@dataclass(frozen=True)
class RunItem:
    """One grouped work item — the slot JSONL shape ``build_grouped_jsonl`` emits.

    Fields mirror ``{repo, number, title, types[], type, detail,
    all_numbers[]}``; unknown fields are preserved in ``raw`` (the gate adds
    fields occasionally and the executor must not become a schema
    bottleneck). ``raw_line`` keeps the original JSON text for the latency
    ledger (worker.sh passes ``$item_json`` through verbatim).
    """

    repo: str
    number: int | str | None
    title: str
    detail: str
    types: tuple[str, ...]
    all_numbers: tuple[str, ...]
    type_label: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    raw_line: str = ""

    @property
    def number_str(self) -> str:
        """The ``jq -r '.number'`` rendering (``null`` for a missing number)."""
        return "null" if self.number is None else str(self.number)

    @classmethod
    def from_grouped_json(cls, line: str) -> RunItem:
        """Parse one grouped-JSONL line; raises on invalid JSON/shape."""
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("grouped item is not a JSON object")
        types_raw = data.get("types")
        if not isinstance(types_raw, list) or not types_raw:
            single = data.get("type")
            types_raw = [single] if isinstance(single, str) and single else []
        types = tuple(str(t) for t in types_raw)
        all_numbers_raw = data.get("all_numbers")
        if not isinstance(all_numbers_raw, list) or not all_numbers_raw:
            number = data.get("number")
            all_numbers_raw = [number] if number is not None else []
        return cls(
            repo=str(data.get("repo", "") or ""),
            number=data.get("number"),
            title=str(data.get("title", "") or ""),
            detail=str(data.get("detail", "") or ""),
            types=types,
            all_numbers=tuple(str(n) for n in all_numbers_raw),
            type_label=str(data.get("type") or "+".join(types)),
            raw=data,
            raw_line=line,
        )

    def to_merge_lifecycle_item(self) -> WorkItem:
        return WorkItem(
            repo=self.repo,
            number=self.number if self.number is not None else "",
            types=self.types,
        )


# --- Config (design §4.3) ---


@dataclass(frozen=True)
class RunItemConfig:
    """Scalar policy for one run — agent identity, allowlists, paths, tiers.

    Everything defaults to the conventional (Bob-era) values so a missing
    config file produces exactly today's behavior; nothing requires the
    workspace to be Bob's (identity strings default to neutral values and
    MUST be set by the consuming agent's config for prompt parity).
    """

    workspace: Path

    # Identity / prompt parameters (see ItemPromptParams for what each maps to)
    author: str = ""
    agent_name: str = "Agent"
    operator_name: str = "the operator"
    twitter_handle: str = ""
    forum_handle: str = ""
    peer_agents: str = "other agents"
    agent_msg_policy_note: str = ""

    # Merge-lifecycle policy (step-1 LifecycleConfig values)
    primary_repo: str = ""
    greptile_repos_pattern: str = ""

    # Self-merge / wait-merge allowlists (space-separated repo words, the
    # env-var format the bash uses). Empty = fail-closed (the self-merge
    # check treats an empty WORKSPACE_REPO as no allowlist).
    self_merge_repos: str = ""
    self_merge_allowed_paths: str = ""
    wait_merge_auto_enabled_repos: str = ""

    # Timeout tiers (project-monitoring.sh:513-528)
    default_timeout: int = 900
    default_time_desc: str = "~10 minutes"
    assigned_issue_timeout: int = 1500
    assigned_issue_time_desc: str = "~20 minutes"
    greptile_fix_timeout: int = 2700
    greptile_fix_time_desc: str = "~35 minutes"

    # Paths (workspace-relative defaults resolved by the CLI/loader)
    monitoring_rules_file: Path | None = None
    records_dir: Path | None = None  # None → per-run temp dir
    dispatch_ledger: Path | None = (
        None  # None → workspace/state/project-monitoring-dispatch.jsonl
    )
    wait_merge_gate_log: Path | None = (
        None  # None → workspace/state/pr-address-wait-and-merge/gates.jsonl
    )
    backend_quota_dir: Path | None = None  # None → workspace/state/backend-quota

    # Activity-gate state promotion dirs (project-monitoring.sh:234-235).
    # NOTE(parity): the /tmp/bob-* defaults match today's brain values so a
    # missing config file is behavior-identical; other agents override.
    state_dir: Path = Path("/tmp/bob-project-monitoring-state")
    pending_state_dir: Path = Path("/tmp/bob-project-monitoring-state-pending")

    # Slot lock (derive_lock_config; lib.sh:232-245). Same stem = same lock
    # files as the bash path, so bash and runloops slots for one slot key
    # serialize against each other during the step-5 A/B.
    lock_dir: Path = Path("/tmp")
    lock_stem: str = "bob-project-monitoring"
    lock_history: Path = Path("/tmp/gptme-lock-history.log")

    # Backend session-state roots (trajectory resolution; None → $HOME-derived)
    cc_projects_dir: Path | None = None
    cc_credentials_path: Path | None = None
    copilot_state_dir: Path | None = None
    codex_sessions_dir: Path | None = None

    # Ambient-harness env tagging (project-monitoring.sh:417-422). Empty =
    # set nothing; the brain config sets "BOB_AMBIENT_HARNESS".
    ambient_harness_env: str = ""

    poll_budget_sec: int = 1800
    claim_ttl_minutes: int = 60

    def resolved(self, name: str, default_rel: str) -> Path:
        value = getattr(self, name)
        if value is not None:
            return Path(value)
        return self.workspace / default_rel

    @property
    def resolved_dispatch_ledger(self) -> Path:
        return self.resolved(
            "dispatch_ledger", "state/project-monitoring-dispatch.jsonl"
        )

    @property
    def resolved_wait_merge_gate_log(self) -> Path:
        return self.resolved(
            "wait_merge_gate_log", "state/pr-address-wait-and-merge/gates.jsonl"
        )

    @property
    def resolved_backend_quota_dir(self) -> Path:
        return self.resolved("backend_quota_dir", "state/backend-quota")

    @property
    def resolved_monitoring_rules_file(self) -> Path:
        if self.monitoring_rules_file is not None:
            return Path(self.monitoring_rules_file)
        return self.workspace / "scripts/runs/github/monitoring-rules.md"

    @property
    def resolved_cc_projects_dir(self) -> Path:
        return Path(self.cc_projects_dir or (Path.home() / ".claude" / "projects"))

    @property
    def resolved_cc_credentials_path(self) -> Path:
        return Path(
            self.cc_credentials_path or (Path.home() / ".claude" / ".credentials.json")
        )

    @property
    def resolved_copilot_state_dir(self) -> Path:
        return Path(
            self.copilot_state_dir or (Path.home() / ".copilot" / "session-state")
        )

    @property
    def resolved_codex_sessions_dir(self) -> Path:
        return Path(self.codex_sessions_dir or (Path.home() / ".codex" / "sessions"))

    def lifecycle_config(self) -> LifecycleConfig:
        return LifecycleConfig(
            primary_repo=self.primary_repo,
            greptile_repos_pattern=self.greptile_repos_pattern,
        )

    def prompt_params(self, item: RunItem) -> ItemPromptParams:
        return ItemPromptParams(
            repo=item.repo,
            number=item.number_str,
            workspace=str(self.workspace),
            detail=item.detail,
            all_numbers=item.all_numbers,
            author=self.author,
            agent_name=self.agent_name,
            operator_name=self.operator_name,
            twitter_handle=self.twitter_handle,
            forum_handle=self.forum_handle,
            peer_agents=self.peer_agents,
            agent_msg_policy_note=self.agent_msg_policy_note,
            poll_budget_sec=self.poll_budget_sec,
        )


# --- Hooks (design §4.3: brain-side scripts + injected collaborators) ---


@dataclass
class RunItemHooks:
    """Injectable side-effect boundary.

    Command hooks (``Sequence[str]`` argv prefixes) wrap agent-side scripts;
    every one is optional-with-degradation matching the bash ``[ -f ]`` /
    ``[ -x ]`` guards — a missing hook skips that step. Callable hooks are
    the same collaborators the step-3 worker_records shims inject
    (``post_session``, ``make_record``, worker-result builders, latency
    recorder); the CLI assembles them by dynamic import with graceful
    degradation, mirroring the heredocs' import-failure semantics.
    """

    runner: Sequence[str] = ()
    merge_lifecycle_io: MergeLifecycleIO | None = None
    sysprompt_builder: Sequence[str] | None = None
    delivery_check: Sequence[str] | None = None
    wait_merge_gate: Sequence[str] | None = None
    wait_merge_helper: Sequence[str] | None = None
    arc_manager: Sequence[str] | None = None
    assigned_issue_ack: Sequence[str] | None = None
    claim_tool: Sequence[str] | None = None
    legacy_record_append: Sequence[str] | None = None
    # Post-run summary seam (# NOTE(step5) item 3): invoked once after the
    # item loop with env PM_ITEM_COUNT/PM_ITEM_SUCCESSES/PM_ITEM_FAILURES/
    # PM_RATE_LIMITED/PM_RECORDS_DIR/PM_START_COMMIT/PM_DURATION_SECONDS/
    # PM_BACKEND/PM_MODEL/PM_LANE.
    post_run: Sequence[str] | None = None

    git_pull: Callable[[], Any] | None = None
    self_merge_gate_available: bool = True
    greptile_helper_available: bool = True

    # In-process collaborators (worker_records injection points)
    post_session: Callable[..., Any] | None = None
    make_store: Callable[[Path], Any] | None = None
    make_record: Callable[..., Mapping[str, Any]] | None = None
    build_worker_result: Callable[..., dict[str, Any]] | None = None
    write_worker_result: Callable[[Path, Mapping[str, Any]], Any] | None = None
    load_worker_result: Callable[[Path], Mapping[str, Any] | None] | None = None
    append_latency_records: Callable[..., Any] | None = None
    upgrade_outcome: Callable[[dict[str, Any]], Any] | None = None
    capture_latency_context: Callable[..., Any] | None = None
    fetch_pr_snapshot: Callable[[str, int], dict[str, str]] | None = None

    # Subprocess seam (tests replace with a fake dispatcher)
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run


# --- Pure helpers ---

_SLUG_TRANS = str.maketrans("/# :", "----")


def item_slug(repo: str, number_str: str, index: int) -> str:
    """``printf '%s_%s_%s' repo number idx | tr '/# :' '----'`` (p-m.sh:590)."""
    return f"{repo}_{number_str}_{index}".translate(_SLUG_TRANS)


def derive_session_id(slug: str, run_salt: int | str) -> str:
    """UUID v5 with run-level salt (p-m.sh:592-597) — stable within a run,
    distinct across overlapping runs."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"monitor-{slug}-{run_salt}"))


def predict_cc_trajectory_path(
    projects_dir: Path, workspace: Path | str, session_id: str
) -> str:
    """The deterministic per-item CC trajectory path (p-m.sh:601-606).

    ``~/.claude/projects/-<workspace with / → ->/<session>.jsonl``.
    """
    ws = str(workspace)
    ws = ws.removeprefix("/").replace("/", "-")
    return str(projects_dir / f"-{ws}" / f"{session_id}.jsonl")


def timeout_tier(
    types: Sequence[str], has_greptile_fix: bool, config: RunItemConfig
) -> tuple[int, str]:
    """Complexity-based timeout tiers (p-m.sh:513-528); order matters:
    assigned_issue wins over the greptile-fix tier."""
    if "assigned_issue" in types:
        return config.assigned_issue_timeout, config.assigned_issue_time_desc
    if "pr_update" in types and has_greptile_fix:
        return config.greptile_fix_timeout, config.greptile_fix_time_desc
    return config.default_timeout, config.default_time_desc


def issue_coordination_key(
    types: Sequence[str], repo: str, number: int | str | None
) -> str | None:
    """``pm_issue_coordination_key_for_item`` (lib.sh:150-166).

    Only assigned_issue/pr_update items with a positive all-digits number
    get a ``github:REPO#NUM`` key; everything else returns ``None``
    (master-CI synthetic run ids are excluded by type).
    """
    if not ({"assigned_issue", "pr_update"} & set(types)):
        return None
    if not repo or number is None:
        return None
    s = str(number)
    if not _DIGITS_RE.fullmatch(s):
        return None
    if int(s) <= 0:
        return None
    return f"github:{repo}#{s}"


def derive_lock_paths(config: RunItemConfig, slot_key: str | None) -> tuple[Path, str]:
    """``derive_lock_config`` (lib.sh:232-245): per-slot lockfile when a slot
    key is present (run-item is always the detached role), else global."""
    if slot_key:
        safe = slot_key.translate(str.maketrans("/#:", "---"))
        return config.lock_dir / f"{config.lock_stem}-{safe}.lock", f"slot:{slot_key}"
    return config.lock_dir / f"{config.lock_stem}.lock", "global"


def resolve_cc_sub_suffix(credentials_path: Path) -> str:
    """Per-sub block-file suffix from the credentials symlink (worker.sh:168-169).

    ``readlink ~/.claude/.credentials.json | sed 's/.*\\.credentials\\.json\\.//'``
    → ``"<sub>-"`` when the symlink target carries a ``.credentials.json.<sub>``
    suffix, else ``""``.
    """
    try:
        target = os.readlink(credentials_path)
    except OSError:
        return ""
    marker = ".credentials.json."
    idx = target.rfind(marker)
    if idx < 0:
        # NOTE(parity): the bash sed leaves the input unchanged when the
        # pattern misses, making the "sub" the whole readlink output — but a
        # non-suffixed symlink target always contains ".credentials.json"
        # without the trailing dot, so in practice the bash produces the
        # basename-ish string only for exotic layouts. Treat no-marker as
        # no-suffix (the observable behavior for real layouts).
        return ""
    sub = target[idx + len(marker) :]
    return f"{sub}-" if sub else ""


def write_rate_limit_block_file(
    quota_dir: Path,
    rate_limit_type: str,
    resets_at: str,
    sub_suffix: str,
    *,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Write the per-sub claude-code block file (worker.sh:159-185).

    ``seven_day_sonnet`` is model-scoped → separate ``...sonnet-rate-limited``
    file preserves opus access. A parseable epoch ``resets_at`` writes that
    time; otherwise +6 hours. Returns ``(path, human message)``.
    """
    now = now or datetime.now(timezone.utc)
    quota_dir.mkdir(parents=True, exist_ok=True)
    if rate_limit_type == "seven_day_sonnet":
        block_file = (
            quota_dir / f"claude-code-{sub_suffix}sonnet-rate-limited-until.txt"
        )
    else:
        block_file = quota_dir / f"claude-code-{sub_suffix}rate-limited-until.txt"
    epoch: int | None = None
    if resets_at and resets_at != "0":
        try:
            epoch = int(float(resets_at))
        except ValueError:
            epoch = None
    if epoch:
        until = datetime.fromtimestamp(epoch, tz=timezone.utc)
        block_file.write_text(_local_iso_seconds(until) + "\n", encoding="utf-8")
        human = until.strftime("%Y-%m-%d %H:%M UTC")
        msg = f"RATE LIMIT: wrote block file — blocked until {human}"
    else:
        until = now + timedelta(hours=6)
        block_file.write_text(_local_iso_seconds(until) + "\n", encoding="utf-8")
        msg = "RATE LIMIT: wrote block file — blocked 6h (reset time unknown)"
    return block_file, msg


# --- Slot lock (bash-parity flock; lib.sh:639-664) ---


class SlotLock:
    """Kernel flock on the exact bash lockfile path, with the bash history format.

    Deliberately NOT :class:`~gptme_runloops.utils.lock.RunLoopLock`: that
    class derives its own ``gptme-<name>.lock`` filename and history format,
    while the step-5 A/B needs bash and runloops slots for the same slot key
    to contend on the *same* ``/tmp/<stem>[-<slot>].lock`` file.
    """

    def __init__(self, lockfile: Path, scope: str, history: Path, backend: str):
        self.lockfile = lockfile
        self.scope = scope
        self.history = history
        self.backend = backend
        self._fd: int | None = None
        self._acquired_at = 0

    def acquire(self) -> bool:
        fd = os.open(self.lockfile, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        self._acquired_at = int(time.time())
        self._append_history(
            f"{self._acquired_at}|ACQUIRED|{os.getpid()}|project-monitoring|{self.backend}|"
        )
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        duration = int(time.time()) - self._acquired_at
        self._append_history(
            f"{int(time.time())}|RELEASED|{os.getpid()}|project-monitoring|{self.backend}|{duration}"
        )
        try:
            os.ftruncate(self._fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def _append_history(self, line: str) -> None:
        try:
            with self.history.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# --- State promotion (lib.sh:611-631; p-m.sh:695-704) ---


def promote_item_state(
    config: RunItemConfig, repo: str, number: int | str | None
) -> None:
    """Copy the item's pending activity-gate state files to the real state dir."""
    import shutil

    pending = config.pending_state_dir
    state = config.state_dir
    if not pending.is_dir():
        return
    state.mkdir(parents=True, exist_ok=True)
    repo_safe = str(repo).replace("/", "-")
    is_zero = str(number) == "0"
    if is_zero:
        patterns = ["notif-*.state"]
    else:
        patterns = [
            f"{repo_safe}-pr-{number}*.state",
            f"{repo_safe}-issue-{number}*.state",
        ]
    patterns.append(f"{repo_safe}-master-ci.state")
    for pattern in patterns:
        for f in pending.glob(pattern):
            if f.is_file():
                shutil.copy(f, state / f.name)


def promote_notification_states(config: RunItemConfig) -> None:
    """Promote ALL pending notif state files at end of run (p-m.sh:695-704).

    Notification state files use GitHub API ids, not repo#number, so the
    per-item promotion never copies them; without this the gate re-emits the
    same notifications every cycle.
    """
    import shutil

    pending = config.pending_state_dir
    if not pending.is_dir():
        return
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for f in pending.glob("notif-*.state"):
        if f.is_file():
            shutil.copy(f, config.state_dir / f.name)


# --- Trajectory resolution (worker.sh:196-300; design §4.4) ---


def snapshot_copilot_dirs(state_dir: Path) -> set[str] | None:
    """Pre-dispatch snapshot of copilot session-state dir names (worker.sh:52-59)."""
    if not state_dir.is_dir():
        return None
    return {p.name for p in state_dir.iterdir()}


def snapshot_codex_rollouts(sessions_dir: Path) -> set[str] | None:
    """Pre-dispatch snapshot of codex rollout file paths (worker.sh:63-70)."""
    if not sessions_dir.is_dir():
        return None
    return {str(p) for p in sessions_dir.rglob("rollout-*.jsonl")}


def resolve_backend_trajectory(
    backend: str,
    session_id: str,
    *,
    predicted: str,
    started_epoch: int,
    copilot_state_dir: Path,
    codex_sessions_dir: Path,
    copilot_pre: set[str] | None,
    codex_pre: set[str] | None,
    tmp_dir: Path = Path("/tmp"),
) -> str:
    """Resolve the session's real trajectory after the runner returns.

    Behavior-identical to worker.sh:196-300 for all four backends; returns
    the (possibly unchanged) trajectory path string.
    """
    trajectory = predicted

    if backend == "claude-code" and session_id:
        ref = tmp_dir / f"cc-session-log-ref-{session_id}.txt"
        if ref.is_file():
            stream_log = ref.read_text(encoding="utf-8", errors="replace").strip()
            stream_path = Path(stream_log) if stream_log else None
            if (
                stream_path
                and stream_path.is_file()
                and stream_path.stat().st_size > CC_TRAJECTORY_MIN_BYTES
            ):
                trajectory = str(stream_path)
                _log(
                    f"Found monitoring trajectory (stream-json, {stream_path.stat().st_size}B): {trajectory}"
                )

    if backend == "grok-build" and not trajectory and session_id:
        ref = tmp_dir / f"grok-build-session-log-ref-{session_id}.txt"
        if ref.is_file():
            stream_log = ref.read_text(encoding="utf-8", errors="replace").strip()
            stream_path = Path(stream_log) if stream_log else None
            if (
                stream_path
                and stream_path.is_file()
                and stream_path.stat().st_size > GROK_TRAJECTORY_MIN_BYTES
            ):
                trajectory = str(stream_path)
                _log(
                    f"Found monitoring trajectory (grok-build, {stream_path.stat().st_size}B): {trajectory}"
                )
            try:
                ref.unlink()
            except OSError:
                pass

    if backend == "copilot-cli" and not trajectory and copilot_pre is not None:
        post = snapshot_copilot_dirs(copilot_state_dir) or set()
        best: Path | None = None
        best_mtime = -1
        for name in sorted(post - copilot_pre):
            candidate = copilot_state_dir / name / "events.jsonl"
            if not candidate.is_file():
                continue
            mtime = int(candidate.stat().st_mtime)
            if mtime >= started_epoch and mtime > best_mtime:
                best = candidate
                best_mtime = mtime
        if best is not None:
            trajectory = str(best)
            _log(
                f"Found monitoring trajectory (copilot, {best.stat().st_size}B): {trajectory}"
            )

    if backend == "codex" and not trajectory and codex_pre is not None:
        post = snapshot_codex_rollouts(codex_sessions_dir) or set()
        best = None
        best_mtime = -1
        for path_str in sorted(post - codex_pre):
            candidate = Path(path_str)
            if not candidate.is_file():
                continue
            mtime = int(candidate.stat().st_mtime)
            if mtime > best_mtime:
                best = candidate
                best_mtime = mtime
        if best is not None:
            trajectory = str(best)
            _log(
                f"Found monitoring trajectory (codex, {best.stat().st_size}B): {trajectory}"
            )

    return trajectory


# --- Plan (pure; design §4.5) ---


@dataclass(frozen=True)
class ArcInfo:
    arc_id: str
    hint: str
    sessions: int


@dataclass
class ItemPlan:
    """Everything decided before side effects, for one item."""

    index: int
    repo: str
    number: str
    title: str
    types: tuple[str, ...]
    skip_item: bool
    lifecycle_decisions: list[dict[str, Any]]
    dry_run_intents: list[str]
    instruction_kind: str | None
    prompt: str
    timeout: int
    time_desc: str
    backend: str
    model: str
    record_model: str
    slug: str
    session_id: str
    record_file: str
    trajectory_path: str
    claim_mode: str
    claim_key: str | None
    claim_agent: str | None
    ack_intent: bool
    arc_id: str | None
    runner_argv: list[str]
    runner_env: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "types": list(self.types),
            "skip_item": self.skip_item,
            "lifecycle_decisions": self.lifecycle_decisions,
            "dry_run_intents": self.dry_run_intents,
            "instruction_kind": self.instruction_kind,
            "timeout": self.timeout,
            "time_desc": self.time_desc,
            "backend": self.backend,
            "model": self.model,
            "slug": self.slug,
            "session_id": self.session_id,
            "record_file": self.record_file,
            "trajectory_path": self.trajectory_path,
            "claim_mode": self.claim_mode,
            "claim_key": self.claim_key,
            "claim_agent": self.claim_agent,
            "ack_intent": self.ack_intent,
            "arc_id": self.arc_id,
            "runner_argv": self.runner_argv,
            "runner_env": sorted(self.runner_env),
            "prompt_chars": len(self.prompt),
            "prompt": self.prompt,
        }


@dataclass
class ExecutionPlan:
    """The full ``--dry-run`` output — the §5.1 static-parity surface."""

    workspace: str
    work_file: str
    lane: str
    dispatch_id: str
    slot_key: str
    backend: str
    model: str
    claim_mode: str
    lock_file: str
    lock_scope: str
    sysprompt_argv: list[str] | None
    items: list[ItemPlan] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {
            "workspace": self.workspace,
            "work_file": self.work_file,
            "lane": self.lane,
            "dispatch_id": self.dispatch_id,
            "slot_key": self.slot_key,
            "backend": self.backend,
            "model": self.model,
            "claim_mode": self.claim_mode,
            "lock_file": self.lock_file,
            "lock_scope": self.lock_scope,
            "sysprompt_argv": self.sysprompt_argv,
            "items": [item.to_dict() for item in self.items],
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def plan_item(
    item: RunItem,
    *,
    index: int,
    config: RunItemConfig,
    backend: str,
    model: str,
    monitoring_rules: str,
    lifecycle: LifecycleResult,
    arc: ArcInfo | None,
    run_salt: int | str,
    records_dir: str | Path,
    claim_mode: str = "acquire",
    runner: Sequence[str] = (),
    sysprompt_file: str = "",
    dry_run_intents: Sequence[str] = (),
) -> ItemPlan:
    """PURE: fold the item + observed lifecycle/arc state into an ItemPlan.

    Subsumes inventory rows 6-10: investigate instructions, main prompt
    skeleton, timeout tiers, uuid5 session id, mention constraint, claim key,
    record/trajectory paths, runner argv.
    """
    params = config.prompt_params(item)

    greptile_fix = ""
    instruction_kind = lifecycle.instructions.value if lifecycle.instructions else None
    if lifecycle.instructions is not None:
        greptile_fix = render_instruction(
            lifecycle.instructions, params.to_prompt_context()
        ).rstrip("\n")

    timeout, time_desc = timeout_tier(item.types, bool(greptile_fix), config)

    mention = ""
    if is_direct_mention(item.detail):
        mention = render_mention_constraint(params)

    arc_context = ""
    if arc is not None:
        arc_context = render_arc_context(
            params, arc_id=arc.arc_id, arc_hint=arc.hint, arc_sessions=arc.sessions
        )

    claim_key = issue_coordination_key(item.types, item.repo, item.number)
    slug = item_slug(item.repo, item.number_str, index)
    session_id = derive_session_id(slug, run_salt)
    claim_agent = f"project-monitoring-{backend or 'unknown'}-{session_id}"

    preheld_block = ""
    if claim_mode == "preheld" and claim_key:
        preheld_block = render_preheld_claim_block(claim_key)

    investigate = build_investigate(item.types, params)
    prompt = render_main_prompt(
        params,
        item_type=item.type_label or "+".join(item.types),
        title=item.title,
        investigate=investigate,
        monitoring_rules=monitoring_rules,
        time_desc=time_desc,
        greptile_fix_instructions=greptile_fix,
        arc_context=arc_context,
        mention_constraint=mention,
        preheld_block=preheld_block,
    )

    record_file = str(Path(records_dir) / f"{slug}.json")
    trajectory_path = ""
    if backend == "claude-code":
        trajectory_path = predict_cc_trajectory_path(
            config.resolved_cc_projects_dir, config.workspace, session_id
        )

    runner_argv = [
        *runner,
        "--backend",
        backend,
        "--no-lock",
        "--no-pull",
        "--no-grade",
        "--sysprompt-file",
        sysprompt_file,
        "--timeout",
        str(timeout),
    ]
    if model:
        runner_argv += ["--model", model]
    runner_argv.append(prompt)

    runner_env: dict[str, str] = {}
    if backend == "claude-code":
        runner_env["CC_SESSION_ID"] = session_id
    elif backend == "grok-build":
        runner_env["GROK_BUILD_SESSION_ID"] = session_id

    return ItemPlan(
        index=index,
        repo=item.repo,
        number=item.number_str,
        title=item.title,
        types=item.types,
        skip_item=lifecycle.skip_item,
        lifecycle_decisions=[
            {
                "action": d.action.value,
                "reason": d.reason,
                "instructions": d.instructions.value if d.instructions else None,
            }
            for d in lifecycle.decisions
        ],
        dry_run_intents=list(dry_run_intents),
        instruction_kind=instruction_kind,
        prompt=prompt,
        timeout=timeout,
        time_desc=time_desc,
        backend=backend,
        model=model,
        record_model=model or "unknown",
        slug=slug,
        session_id=session_id,
        record_file=record_file,
        trajectory_path=trajectory_path,
        claim_mode=claim_mode,
        claim_key=claim_key,
        claim_agent=claim_agent if claim_key else None,
        ack_intent=(
            "assigned_issue" in item.types
            and "pending_reply_followup" not in item.detail
        ),
        arc_id=arc.arc_id if arc else None,
        runner_argv=runner_argv,
        runner_env=runner_env,
    )


# --- Dry-run lifecycle IO (reads pass through, writes become intents) ---


@dataclass
class DryRunMergeLifecycleIO:
    """Wraps a real IO: read-only calls pass through; mutations are recorded.

    ``self_merge`` optimistically returns True so the plan reflects the
    skip-session outcome an eligible gate verdict leads to in a real run.
    """

    inner: MergeLifecycleIO
    intents: list[str] = field(default_factory=list)

    def self_merge_check(self, repo: str, number: int | str) -> SelfMergeCheckResult:
        return self.inner.self_merge_check(repo, number)

    def self_merge(self, repo: str, number: int | str) -> bool:
        self.intents.append(f"would self-merge {repo}#{number}")
        return True

    def greptile_status(self, repo: str, number: int | str) -> str:
        return self.inner.greptile_status(repo, number)

    def trigger_review(self, repo: str, number: int | str) -> None:
        self.intents.append(f"would trigger Greptile review on {repo}#{number}")

    def promote_item_state(self, repo: str, number: int | str) -> None:
        self.intents.append(f"would promote item state for {repo}#{number}")


# --- Execution (I/O) ---


@dataclass
class RunItemOutcome:
    """Observed results of one executed item (input to run_post_session)."""

    exit_code: int
    duration_seconds: int
    started_epoch: int
    started_iso: str
    trajectory_path: str
    pr_before_json: str
    latency_context_json: str
    ack_result_json: str
    rate_limited: bool = False
    counted_failure: bool = False


def _find_arc(
    item: RunItem, config: RunItemConfig, hooks: RunItemHooks
) -> ArcInfo | None:
    """``build_arc_context``'s arc lookup (lib.sh:82-113) via the arc_manager hook."""
    if hooks.arc_manager is None:
        return None
    upstream_id = f"github:{item.repo}#{item.number_str}"
    try:
        proc = hooks.run_cmd(
            [*hooks.arc_manager, "find", upstream_id],
            capture_output=True,
            text=True,
            cwd=str(config.workspace),
        )
        raw = (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        arc_id = str(data.get("arc_id", "") or "")
        hint = str(data.get("next_step_hint", "") or "")
        sessions = len(data.get("sessions", []) or [])
    except (json.JSONDecodeError, TypeError):
        return None
    if not arc_id:
        # NOTE(parity): the bash keeps an empty-arc_id result (ARC_CONTEXT
        # renders with '' in the arc_manager write example); an arc find that
        # returns JSON without arc_id is a broken record — treat as no arc.
        return None
    _log(f"  Arc found for {upstream_id} ({sessions} sessions, hint: {hint[:60]}...)")
    return ArcInfo(arc_id=arc_id, hint=hint, sessions=sessions)


def _capture_latency_context(
    item: RunItem, config: RunItemConfig, hooks: RunItemHooks
) -> str:
    """p-m.sh:614-638 — capture latency context; any failure → ``"[]"``."""
    if hooks.capture_latency_context is None:
        return "[]"
    try:
        context = hooks.capture_latency_context(
            repo_root=Path(config.workspace), item=dict(item.raw)
        )
        return json.dumps(context, ensure_ascii=False)
    except Exception:
        return "[]"


def _run_assigned_issue_ack(
    item: RunItem,
    plan: ItemPlan,
    config: RunItemConfig,
    hooks: RunItemHooks,
    latency_context_json: str,
) -> str:
    """p-m.sh:645-665 — early ack for assigned issues; failures tolerated."""
    if not plan.ack_intent or hooks.assigned_issue_ack is None:
        return ""
    try:
        proc = hooks.run_cmd(
            [
                *hooks.assigned_issue_ack,
                "--repo-root",
                str(config.workspace),
                "--repo",
                item.repo,
                "--number",
                item.number_str,
                "--title",
                item.title,
                "--author",
                config.author,
                "--latency-context-json",
                latency_context_json,
            ],
            capture_output=True,
            text=True,
            cwd=str(config.workspace),
        )
        result = (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        result = ""
    if result:
        _log(f"Assigned-issue ack: {result}")
    return result


def _acquire_claim(plan: ItemPlan, config: RunItemConfig, hooks: RunItemHooks) -> bool:
    """``claim_project_monitoring_issue_work`` (lib.sh:168-188).

    Returns True when the item may proceed. ``preheld``/``none`` modes and
    non-claimable items always proceed; ``acquire`` mode runs the claim tool
    and treats ANY failure as denied (fail-closed, like the bash ``if``).
    """
    if plan.claim_mode != "acquire" or plan.claim_key is None:
        return True
    if hooks.claim_tool is None:
        _log(
            f"WARN: no claim tool configured — proceeding unclaimed for {plan.claim_key}"
        )
        return True
    try:
        proc = hooks.run_cmd(
            [
                *hooks.claim_tool,
                "work-claim",
                plan.claim_agent or "project-monitoring-unknown",
                plan.claim_key,
                "--ttl",
                str(config.claim_ttl_minutes),
            ],
            capture_output=True,
            text=True,
            cwd=str(config.workspace),
        )
        granted = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        granted = False
    if granted:
        _log(f"Claimed upstream issue work: {plan.claim_key}")
    else:
        _log(
            f"Skipping item {plan.index}: coordination claim denied for {plan.claim_key}"
        )
    return granted


def _abandon_claim(plan: ItemPlan, config: RunItemConfig, hooks: RunItemHooks) -> None:
    """``abandon_project_monitoring_issue_work_claim`` (lib.sh:190-201)."""
    if (
        plan.claim_mode != "acquire"
        or plan.claim_key is None
        or hooks.claim_tool is None
    ):
        return
    try:
        hooks.run_cmd(
            [
                *hooks.claim_tool,
                "work-abandon",
                plan.claim_agent or "project-monitoring-unknown",
                plan.claim_key,
            ],
            capture_output=True,
            text=True,
            cwd=str(config.workspace),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def execute_plan(
    plan: ItemPlan,
    item: RunItem,
    config: RunItemConfig,
    hooks: RunItemHooks,
    *,
    ambient_env: dict[str, str] | None = None,
    latency_context_json: str = "[]",
    ack_result_json: str = "",
) -> RunItemOutcome:
    """Run the session for one planned item (worker.sh:41-96 + 100-190).

    Executes the runner subprocess, interprets the exit code, parses a
    confirmed rate-limit rejection (writing the per-sub block file only on a
    confirmed *rejected* event — the 2026-06-14 401-misclassification guard),
    and resolves the backend trajectory.

    ``latency_context_json`` / ``ack_result_json`` are captured by the caller
    BEFORE the coordination claim, preserving the bash ordering
    (p-m.sh:614-676: latency capture → ack → claim → worker).
    """
    started_epoch = int(time.time())
    started_iso = _local_iso_seconds(datetime.now())

    copilot_pre = None
    codex_pre = None
    if plan.backend == "copilot-cli":
        copilot_pre = snapshot_copilot_dirs(config.resolved_copilot_state_dir)
    elif plan.backend == "codex":
        codex_pre = snapshot_codex_rollouts(config.resolved_codex_sessions_dir)

    pr_before_json = ""
    if PR_STATE_TYPES & set(item.types):
        try:
            proc = hooks.run_cmd(
                [
                    "gh",
                    "pr",
                    "view",
                    plan.number,
                    "--repo",
                    plan.repo,
                    "--json",
                    "state,headRefOid,mergeCommit",
                ],
                capture_output=True,
                text=True,
                cwd=str(config.workspace),
            )
            pr_before_json = proc.stdout if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            pr_before_json = ""

    env = os.environ.copy()
    env.update(ambient_env or {})
    env.update(plan.runner_env)
    exit_code = 0
    try:
        proc = hooks.run_cmd(
            plan.runner_argv,
            cwd=str(config.workspace),
            env=env,
            stdin=subprocess.DEVNULL,
        )
        exit_code = proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"WARN: runner invocation failed for item {plan.index}: {exc}")
        exit_code = 1

    duration = int(time.time()) - started_epoch

    rate_limited = False
    counted_failure = False
    if exit_code == 124:
        _log(
            f"WARN: Item {plan.index} timed out after {plan.timeout}s ({plan.time_desc})"
        )
    elif exit_code != 0:
        _log(f"WARN: Item {plan.index} exited with code {exit_code}")
        counted_failure = True
        if plan.backend == "claude-code":
            rate_limited = _handle_cc_rate_limit(plan, config)

    trajectory = plan.trajectory_path
    if not rate_limited:
        trajectory = resolve_backend_trajectory(
            plan.backend,
            plan.session_id,
            predicted=plan.trajectory_path,
            started_epoch=started_epoch,
            copilot_state_dir=config.resolved_copilot_state_dir,
            codex_sessions_dir=config.resolved_codex_sessions_dir,
            copilot_pre=copilot_pre,
            codex_pre=codex_pre,
        )
    else:
        # NOTE(parity): the rate-limit branch deletes the CC stream log +
        # ref (worker.sh:186), so trajectory resolution finds nothing and
        # the predicted stub path stands. Skip resolution to match.
        pass

    return RunItemOutcome(
        exit_code=exit_code,
        duration_seconds=duration,
        started_epoch=started_epoch,
        started_iso=started_iso,
        trajectory_path=trajectory,
        pr_before_json=pr_before_json,
        latency_context_json=latency_context_json,
        ack_result_json=ack_result_json,
        rate_limited=rate_limited,
        counted_failure=counted_failure,
    )


def _handle_cc_rate_limit(
    plan: ItemPlan, config: RunItemConfig, *, tmp_dir: Path = Path("/tmp")
) -> bool:
    """worker.sh:107-189 — CC rate-limit detection on a failed session.

    Prefers the session-specific log ref (#543); requires a CONFIRMED
    *rejected* rate_limit_event before blocking (never a bare
    ``rateLimitType`` grep — a 401 stream mentions the field too).
    Removes the log + ref when the field was present at all, like the bash.
    """
    ref = tmp_dir / f"cc-session-log-ref-{plan.session_id}.txt"
    if not (plan.session_id and ref.is_file()):
        ref = tmp_dir / "cc-last-session-log.txt"
    if not ref.is_file():
        return False
    log_path_str = ref.read_text(encoding="utf-8", errors="replace").strip()
    log_path = Path(log_path_str) if log_path_str else None
    if log_path is None or not log_path.is_file():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if '"rateLimitType"' not in text:
        return False

    rejection = None
    try:
        rejection = parse_rate_limit_rejection(text.splitlines())
    except Exception:
        _log(
            "WARN: worker_records rate-limit parser failed (log parse) — "
            "treating as no confirmed rejection"
        )

    rate_limited = False
    if rejection is not None:
        _log("ERROR: Claude Code subscription rate-limited")
        rate_limited = True
        sub_suffix = resolve_cc_sub_suffix(config.resolved_cc_credentials_path)
        _, msg = write_rate_limit_block_file(
            config.resolved_backend_quota_dir,
            str(rejection.rate_limit_type),
            str(rejection.resets_at),
            sub_suffix,
        )
        sub = sub_suffix.rstrip("-") or "unknown"
        _log(f"{msg} (sub: {sub})")
    else:
        _log(
            "NOTE: log mentioned rateLimitType but no REJECTED rate_limit_event — "
            "not blocking (likely a 401/auth failure, not a rate limit)"
        )
    for p in (log_path, ref):
        try:
            p.unlink()
        except OSError:
            pass
    return rate_limited


# --- Post-session bookkeeping (worker.sh:192-664; composes worker_records) ---


def run_post_session(
    plan: ItemPlan,
    item: RunItem,
    outcome: RunItemOutcome,
    config: RunItemConfig,
    hooks: RunItemHooks,
) -> None:
    """One Python composition of the six worker.sh heredoc shims, in order.

    Every step is non-fatal (WARN + continue), matching the bash ``|| true``
    tolerance — EXCEPT delivery-extraction total death, which records the
    delivery outcome as ``failed`` (never ``handled``; the step-3 divergence,
    kept).
    """
    record_file = Path(plan.record_file)
    exit_code = outcome.exit_code
    duration = outcome.duration_seconds

    # 1. Primary record write; legacy fallback on any failure (worker.sh:303-377)
    try:
        if hooks.post_session is None or hooks.make_store is None:
            raise RuntimeError("post_session collaborators unavailable")
        write_post_session_record(
            record_file,
            harness=plan.backend,
            model=plan.record_model,
            session_id=plan.session_id,
            exit_code=exit_code,
            duration_seconds=duration,
            item_timeout=plan.timeout,
            trajectory_path=outcome.trajectory_path,
            post_session=hooks.post_session,
            make_store=hooks.make_store,
        )
    except Exception:
        _log(f"WARN: post_session failed for item {plan.index}, using legacy fallback")
        outcome_str = fallback_outcome(exit_code)
        if hooks.legacy_record_append is not None:
            try:
                hooks.run_cmd(
                    [
                        *hooks.legacy_record_append,
                        "append",
                        "--harness",
                        plan.backend,
                        "--model",
                        plan.record_model,
                        "--run-type",
                        "monitoring",
                        "--category",
                        "pm-react",
                        "--outcome",
                        outcome_str,
                        "--duration",
                        str(duration),
                    ],
                    capture_output=True,
                    text=True,
                    cwd=str(config.workspace),
                )
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            if hooks.make_record is None:
                raise RuntimeError("make_record collaborator unavailable")
            write_fallback_session_record(
                record_file,
                harness=plan.backend,
                model=plan.record_model,
                outcome=outcome_str,
                session_id=plan.session_id,
                exit_code=exit_code,
                duration_seconds=duration,
                item_timeout=plan.timeout,
                trajectory_path=outcome.trajectory_path,
                make_record=hooks.make_record,
            )
        except Exception:
            _log(
                "WARN: worker_records fallback session-record write step failed "
                f"for {plan.repo}#{plan.number}"
            )

    is_pr_item = bool(PR_STATE_TYPES & set(item.types))

    # 2. PR-state diff (worker.sh:379-408)
    if is_pr_item:
        try:
            update_record_pr_state(
                record_file,
                repo=plan.repo,
                number=int(plan.number),
                before_json=outcome.pr_before_json,
                cwd=str(config.workspace),
                fetch=hooks.fetch_pr_snapshot,
                upgrade_outcome=hooks.upgrade_outcome,
            )
        except Exception:
            _log(
                f"WARN: worker_records PR-state diff step failed for {plan.repo}#{plan.number}"
            )

    # 3. Worker-result manifest (worker.sh:410-438)
    try:
        if (
            hooks.build_worker_result is None
            or hooks.write_worker_result is None
            or hooks.load_worker_result is None
        ):
            raise RuntimeError("worker-result collaborators unavailable")
        write_worker_result_manifest(
            record_file,
            repo=plan.repo,
            number=int(plan.number),
            session_id=plan.session_id,
            exit_code=exit_code,
            duration_seconds=duration,
            model=plan.record_model,
            item_types=list(item.types),
            build_worker_result=hooks.build_worker_result,
            write_worker_result=hooks.write_worker_result,
            load_worker_result=hooks.load_worker_result,
        )
    except Exception:
        _log(
            f"WARN: worker_records worker-result manifest step failed for {plan.repo}#{plan.number}"
        )

    # 4. Delivery post-condition (worker.sh:440-515)
    delivery_outcome = "handled"
    needs_fallback = "false"
    fallback_posted = "false"
    if item.repo and item.number is not None and hooks.delivery_check is not None:
        try:
            try:
                proc = hooks.run_cmd(
                    [
                        *hooks.delivery_check,
                        "--repo",
                        plan.repo,
                        "--number",
                        plan.number,
                        "--since",
                        outcome.started_iso,
                        "--require-thread-reply",
                        "--post-fallback-reply",
                        "--session-id",
                        plan.session_id,
                    ],
                    capture_output=True,
                    text=True,
                    cwd=str(config.workspace),
                )
                raw = proc.stdout if proc.returncode == 0 else '{"outcome":"handled"}'
            except (OSError, subprocess.SubprocessError):
                raw = '{"outcome":"handled"}'
            delivery_outcome = normalize_delivery_outcome(
                extract_delivery_field(raw, "outcome")
            )
            needs_fallback = re.sub(
                r"[ \t\n\r\f\v]+",
                "",
                extract_delivery_field(raw, "needs_fallback_reply"),
            )
            fallback_posted = re.sub(
                r"[ \t\n\r\f\v]+",
                "",
                extract_delivery_field(raw, "fallback_reply_posted"),
            )
        except Exception:
            # NOTE(divergence, kept from step 3): total extractor death fails
            # LOUD as outcome "failed" (never "handled") so a broken delivery
            # path surfaces in the latency ledger.
            _log(
                "WARN: delivery-field extraction failed entirely — recording "
                "delivery outcome 'failed' (not 'handled')"
            )
            delivery_outcome = "failed"
            needs_fallback = "false"
            fallback_posted = "false"
        if delivery_outcome == "orphan_no_delivery":
            _log(
                f"WARN: PM delivery post-condition FAILED — {plan.repo}#{plan.number}: "
                "session exited without a thread reply"
            )

    latency_outcome = compute_latency_outcome(
        delivery_outcome,
        exit_code,
        needs_fallback_reply=needs_fallback,
        fallback_reply_posted=fallback_posted,
    )

    # 5. Latency-ledger append (worker.sh:526-550)
    try:
        if hooks.append_latency_records is None:
            raise RuntimeError("append_latency_records collaborator unavailable")
        append_worker_latency_records(
            repo_root=config.workspace,
            item_json=item.raw_line or json.dumps(dict(item.raw), ensure_ascii=False),
            latency_context_json=outcome.latency_context_json,
            ack_result_json=outcome.ack_result_json,
            handled_at=outcome.started_iso,
            session_id=plan.session_id,
            outcome=latency_outcome,
            append_latency_records=hooks.append_latency_records,
        )
    except Exception:
        _log(
            f"WARN: worker_records latency-ledger append step failed for {plan.repo}#{plan.number}"
        )

    # 6. Wait-and-merge gate (worker.sh:552-616)
    if is_pr_item and exit_code == 0 and hooks.wait_merge_gate is not None:
        auto_repos = [r for r in config.wait_merge_auto_enabled_repos.split() if r]
        gate_args = [
            *hooks.wait_merge_gate,
            "--repo",
            plan.repo,
            "--pr",
            plan.number,
            "--record",
            str(record_file),
            "--session-start",
            outcome.started_iso,
        ]
        for repo_word in auto_repos:
            gate_args += ["--auto-enabled-repo", repo_word]
        gate_json = ""
        gate_status = 0
        try:
            proc = hooks.run_cmd(
                gate_args, capture_output=True, text=True, cwd=str(config.workspace)
            )
            gate_json = proc.stdout or ""
            gate_status = proc.returncode
        except (OSError, subprocess.SubprocessError):
            gate_status = 2
        try:
            entry = build_wait_merge_gate_entry(
                gate_json,
                timestamp=_now_utc_iso(),
                repo=plan.repo,
                pr_number=int(plan.number),
                item_types=list(item.types),
                session_id=plan.session_id,
                session_start=outcome.started_iso,
                gate_exit_code=gate_status,
            )
            append_wait_merge_gate_log(config.resolved_wait_merge_gate_log, entry)
        except Exception:
            _log(
                f"WARN: worker_records wait-and-merge gate log step failed for {plan.repo}#{plan.number}"
            )
        if gate_status == 0:
            _log(f"Auto wait-and-merge gate passed: {gate_json}")
            if hooks.wait_merge_helper is not None:
                helper_env = os.environ.copy()
                helper_env.update(
                    {
                        "WORKSPACE_REPO": config.self_merge_repos,
                        "SELF_MERGE_ALLOWED_PATHS": config.self_merge_allowed_paths,
                        "PR_ADDRESS_TRIGGER": "auto-monitoring",
                    }
                )
                helper_exit = 0
                try:
                    proc = hooks.run_cmd(
                        [*hooks.wait_merge_helper, "--repo", plan.repo, plan.number],
                        cwd=str(config.workspace),
                        env=helper_env,
                    )
                    helper_exit = proc.returncode
                except (OSError, subprocess.SubprocessError):
                    helper_exit = 1
                if helper_exit in (2, 3):
                    _log(
                        f"Auto wait-and-merge helper exited {helper_exit}; leaving "
                        "follow-up to the next monitoring cycle."
                    )
                elif helper_exit != 0:
                    _log(
                        f"WARN: auto wait-and-merge helper exited {helper_exit} "
                        f"for {plan.repo}#{plan.number}"
                    )
        elif gate_status == 2:
            _log(
                f"WARN: auto wait-and-merge gate lookup failed for "
                f"{plan.repo}#{plan.number}: {gate_json}"
            )

    # 7. Arc continuation record + auto-close (worker.sh:618-659)
    if plan.arc_id and hooks.arc_manager is not None:
        types_text = "\n".join(item.types)
        progress = (
            f"project-monitoring handled {types_text} for {plan.repo}#{plan.number}"
        )
        hint = (
            f"Review the latest monitoring result for {plan.repo}#{plan.number} "
            "and continue from there."
        )
        if exit_code == 124:
            progress = f"project-monitoring timed out on {types_text} for {plan.repo}#{plan.number}"
            hint = (
                f"Re-run the monitoring lane for {plan.repo}#{plan.number} after "
                "checking the timeout cause."
            )
        elif exit_code != 0:
            progress = f"project-monitoring failed on {types_text} for {plan.repo}#{plan.number}"
            hint = (
                f"Inspect the failed monitoring run for {plan.repo}#{plan.number} "
                "and retry once the cause is clear."
            )
        try:
            hooks.run_cmd(
                [
                    *hooks.arc_manager,
                    "update",
                    plan.arc_id,
                    "--session-id",
                    plan.session_id,
                    "--progress-delta",
                    progress,
                    "--next-step-hint",
                    hint,
                    "--owner-lane",
                    "pm-react",
                ],
                capture_output=True,
                text=True,
                cwd=str(config.workspace),
            )
        except (OSError, subprocess.SubprocessError):
            pass

        pr_state_after = ""
        if record_file.is_file():
            pr_state_after = read_record_pr_state_after(record_file)
        if not pr_state_after and item.repo and item.number is not None:
            try:
                proc = hooks.run_cmd(
                    [
                        "gh",
                        "pr",
                        "view",
                        plan.number,
                        "--repo",
                        plan.repo,
                        "--json",
                        "state",
                        "--jq",
                        ".state",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=str(config.workspace),
                )
                if proc.returncode == 0:
                    pr_state_after = (proc.stdout or "").strip().upper()
            except (OSError, subprocess.SubprocessError):
                pass
        if pr_state_after == "MERGED":
            _log(
                f"  PR {plan.repo}#{plan.number} is MERGED — auto-closing arc {plan.arc_id}"
            )
            try:
                hooks.run_cmd(
                    [*hooks.arc_manager, "close", plan.arc_id],
                    capture_output=True,
                    text=True,
                    cwd=str(config.workspace),
                )
            except (OSError, subprocess.SubprocessError):
                pass

    # 8. State promotion (worker.sh:661-663)
    promote_item_state(config, item.repo, item.number)
    _log(f"=== Item {plan.index} complete ===")


# --- Ledger ---


def _append_ledger(
    config: RunItemConfig,
    phase: str,
    lane: str,
    dispatch_id: str,
    work_file: str,
    note: str,
    *,
    successes: int | None = None,
    failures: int | None = None,
    duration_seconds: int | None = None,
) -> None:
    try:
        append_full_ledger_entry(
            config.resolved_dispatch_ledger,
            phase=phase,
            lane=lane,
            dispatch_id=dispatch_id or None,
            unit_name=dispatch_id or None,
            work_file=work_file or None,
            note=note or None,
            successes=successes,
            failures=failures,
            duration_seconds=duration_seconds,
        )
    except Exception as exc:
        _log(f"WARN: dispatch-ledger append failed ({phase}): {exc}")


# --- Orchestrator ---


def read_work_items(work_file: Path) -> list[RunItem]:
    """Parse the grouped-item JSONL; malformed lines WARN + skip (never crash
    the slot — design §8)."""
    items: list[RunItem] = []
    for raw in work_file.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            items.append(RunItem.from_grouped_json(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            _log(f"WARN: skipping malformed work item ({exc}): {raw[:200]}")
    return items


def build_execution_plan(
    work_file: Path,
    config: RunItemConfig,
    hooks: RunItemHooks,
    *,
    backend: str,
    model: str = "",
    lane: str = "mixed",
    dispatch_id: str = "",
    slot_key: str = "",
    claim_mode: str = "acquire",
) -> ExecutionPlan:
    """Resolve decisions + render prompts + assemble the full plan; execute
    nothing (the ``--dry-run`` surface, design §5.1).

    Mutating lifecycle I/O is wrapped in :class:`DryRunMergeLifecycleIO`
    (read-only gate/status calls still run; merge/trigger become recorded
    intents, with self-merge assumed to succeed).
    """
    items = read_work_items(work_file)
    lock_file, lock_scope = derive_lock_paths(config, slot_key or None)
    monitoring_rules = _read_monitoring_rules(config)
    records_dir = str(config.records_dir or Path("/tmp") / "run-item-records")

    plan = ExecutionPlan(
        workspace=str(config.workspace),
        work_file=str(work_file),
        lane=lane,
        dispatch_id=dispatch_id,
        slot_key=slot_key,
        backend=backend,
        model=model,
        claim_mode=claim_mode,
        lock_file=str(lock_file),
        lock_scope=lock_scope,
        sysprompt_argv=list(hooks.sysprompt_builder)
        if hooks.sysprompt_builder
        else None,
    )
    run_salt = int(time.time())
    for index, item in enumerate(items, start=1):
        intents: list[str] = []
        lifecycle = LifecycleResult()
        if hooks.merge_lifecycle_io is not None:
            dry_io = DryRunMergeLifecycleIO(hooks.merge_lifecycle_io)
            lifecycle = run_merge_lifecycle(
                item.to_merge_lifecycle_item(),
                config.lifecycle_config(),
                dry_io,
                gate_available=hooks.self_merge_gate_available,
                helper_available=hooks.greptile_helper_available,
                log=_log,
            )
            intents = dry_io.intents
        arc = _find_arc(item, config, hooks)
        plan.items.append(
            plan_item(
                item,
                index=index,
                config=config,
                backend=backend,
                model=model,
                monitoring_rules=monitoring_rules,
                lifecycle=lifecycle,
                arc=arc,
                run_salt=run_salt,
                records_dir=records_dir,
                claim_mode=claim_mode,
                runner=hooks.runner,
                sysprompt_file="<sysprompt>",
                dry_run_intents=intents,
            )
        )
    return plan


def _read_monitoring_rules(config: RunItemConfig) -> str:
    rules_file = config.resolved_monitoring_rules_file
    if rules_file.is_file():
        return rules_file.read_text(encoding="utf-8")
    _log("WARN: monitoring-rules.md not found, using inline fallback")
    return ""


def _ambient_env(config: RunItemConfig, backend: str, model: str) -> dict[str, str]:
    """p-m.sh:417-422 — harness tag for ambient-memory injections."""
    if not config.ambient_harness_env:
        return {}
    if backend == "gptme":
        value = f"gptme:{model or os.environ.get('GPTME_MODEL', 'unknown')}"
    elif backend == "grok-build":
        value = (
            f"grok-build:{model or os.environ.get('GROK_BUILD_MODEL', 'grok-build')}"
        )
    else:
        value = backend
    return {config.ambient_harness_env: value}


def run_work_file(
    work_file: Path,
    config: RunItemConfig,
    hooks: RunItemHooks,
    *,
    backend: str,
    model: str = "",
    lane: str = "mixed",
    dispatch_id: str = "",
    slot_key: str = "",
    claim_mode: str = "acquire",
) -> int:
    """Execute one work-file end-to-end (the detached bash path, §4.7 semantics).

    Returns the run's exit code: 1 for a missing/empty work file, 0 when a
    busy lock skips the run, otherwise the first non-zero session exit code
    (0 when all sessions succeeded / were skipped). Multi-item files process
    sequentially with the rate-limit early break.
    """
    import tempfile

    if not work_file.is_file():
        _log(f"ERROR: work file ('{work_file}') is missing")
        return 1
    items = read_work_items(work_file)
    if not items:
        _log(f"ERROR: work file ('{work_file}') is empty")
        return 1

    _log(
        f"--- Detached dispatch (slot={slot_key or 'none'} lane={lane}): "
        f"{len(items)} pre-computed item(s) ---"
    )
    for item in items:
        _log(f"  {item.type_label}: {item.repo} #{item.number_str} — {item.title}")
    _append_ledger(
        config, "started", lane, dispatch_id, str(work_file), "transient_started"
    )

    lock_file, lock_scope = derive_lock_paths(config, slot_key or None)
    lock = SlotLock(lock_file, lock_scope, config.lock_history, backend)
    if not lock.acquire():
        pid = ""
        try:
            pid = lock_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        _log(
            "No work: another project-monitoring run is active for lock scope "
            f"'{lock_scope}' (PID {pid or 'unknown'})"
        )
        return 0

    run_start = int(time.time())
    sysprompt_path: Path | None = None
    temp_records: tempfile.TemporaryDirectory[str] | None = None
    try:
        if hooks.git_pull is not None:
            try:
                hooks.git_pull()
            except Exception as exc:
                _log(f"WARN: git pull hook failed: {exc}")

        start_commit = ""
        try:
            proc = hooks.run_cmd(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(config.workspace),
            )
            if proc.returncode == 0:
                start_commit = (proc.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            pass

        ambient = _ambient_env(config, backend, model)
        if hooks.sysprompt_builder is not None:
            _log("Building system prompt...")
            env = os.environ.copy()
            env.update(ambient)
            try:
                proc = hooks.run_cmd(
                    list(hooks.sysprompt_builder),
                    capture_output=True,
                    text=True,
                    cwd=str(config.workspace),
                    env=env,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"sysprompt builder exited {proc.returncode}")
                fd, name = tempfile.mkstemp(prefix="run-item-sysprompt.")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(proc.stdout or "")
                sysprompt_path = Path(name)
                _log(f"System prompt: {sysprompt_path.stat().st_size} bytes")
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                _log(
                    f"WARN: system-prompt build failed ({exc}); running without sysprompt file"
                )
                sysprompt_path = None

        monitoring_rules = _read_monitoring_rules(config)

        if config.records_dir is not None:
            records_dir = Path(config.records_dir)
            records_dir.mkdir(parents=True, exist_ok=True)
        else:
            temp_records = tempfile.TemporaryDirectory(prefix="run-item-records.")
            records_dir = Path(temp_records.name)

        group_count = len(items)
        failures = 0
        rate_limited = False
        overall_exit = 0

        for index, item in enumerate(items, start=1):
            _log("")
            _log(
                f"=== Item {index}/{group_count}: {item.type_label} in "
                f"{item.repo} #{item.number_str} ==="
            )

            lifecycle = LifecycleResult()
            if hooks.merge_lifecycle_io is not None:
                lifecycle = run_merge_lifecycle(
                    item.to_merge_lifecycle_item(),
                    config.lifecycle_config(),
                    hooks.merge_lifecycle_io,
                    gate_available=hooks.self_merge_gate_available,
                    helper_available=hooks.greptile_helper_available,
                    log=_log,
                )
            if lifecycle.skip_item:
                _log(f"=== Item {index} complete (self-merged) ===")
                continue

            arc = _find_arc(item, config, hooks)
            plan = plan_item(
                item,
                index=index,
                config=config,
                backend=backend,
                model=model,
                monitoring_rules=monitoring_rules,
                lifecycle=lifecycle,
                arc=arc,
                run_salt=run_start,
                records_dir=records_dir,
                claim_mode=claim_mode,
                runner=hooks.runner,
                sysprompt_file=str(sysprompt_path or ""),
            )
            _log(f"Prompt: {len(plan.prompt)} chars")

            if rate_limited:
                _log(f"Rate-limited: skipping item {index} and remaining items")
                break

            # Bash ordering (p-m.sh:614-676): latency capture and the
            # assigned-issue ack happen BEFORE the coordination claim.
            latency_context_json = _capture_latency_context(item, config, hooks)
            ack_result_json = _run_assigned_issue_ack(
                item, plan, config, hooks, latency_context_json
            )

            if not _acquire_claim(plan, config, hooks):
                _append_ledger(
                    config,
                    "skipped_claimed",
                    lane,
                    dispatch_id,
                    str(work_file),
                    f"coordination_claim_denied:{item.repo}#{item.number_str}",
                )
                continue

            try:
                _log(f"Dispatching item {index}/{group_count}")
                item_outcome = execute_plan(
                    plan,
                    item,
                    config,
                    hooks,
                    ambient_env=ambient,
                    latency_context_json=latency_context_json,
                    ack_result_json=ack_result_json,
                )
                if item_outcome.counted_failure:
                    failures += 1
                if item_outcome.rate_limited:
                    rate_limited = True
                if item_outcome.exit_code != 0 and overall_exit == 0:
                    overall_exit = item_outcome.exit_code
                run_post_session(plan, item, item_outcome, config, hooks)
            finally:
                # bash EXIT-trap parity: the claim is abandoned on every exit
                # path, including SIGTERM (the CLI converts it to SystemExit
                # so this finally fires on RuntimeMaxSec kills).
                _abandon_claim(plan, config, hooks)

        promote_notification_states(config)

        successes = group_count - failures
        duration = int(time.time()) - run_start
        _append_ledger(
            config,
            "completed",
            lane,
            dispatch_id,
            str(work_file),
            "transient_completed",
            successes=successes,
            failures=failures,
            duration_seconds=duration,
        )
        _log(f"Items: {group_count} total, {successes} succeeded, {failures} failed")

        if hooks.post_run is not None:
            env = os.environ.copy()
            env.update(
                {
                    "PM_ITEM_COUNT": str(group_count),
                    "PM_ITEM_SUCCESSES": str(successes),
                    "PM_ITEM_FAILURES": str(failures),
                    "PM_RATE_LIMITED": "1" if rate_limited else "0",
                    "PM_RECORDS_DIR": str(records_dir),
                    "PM_START_COMMIT": start_commit,
                    "PM_DURATION_SECONDS": str(duration),
                    "PM_BACKEND": backend,
                    "PM_MODEL": model,
                    "PM_LANE": lane,
                }
            )
            try:
                hooks.run_cmd(list(hooks.post_run), cwd=str(config.workspace), env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                _log(f"WARN: post-run hook failed: {exc}")

        return overall_exit
    finally:
        if sysprompt_path is not None:
            try:
                sysprompt_path.unlink()
            except OSError:
                pass
        if temp_records is not None:
            temp_records.cleanup()
        lock.release()
