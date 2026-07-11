"""One-item project-monitoring executor primitives.

The workspace retains dispatch and policy.  This module owns portable item
parsing, deterministic planning, and the ``run.sh`` subprocess boundary.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gptme_runloops.merge_lifecycle import InstructionKind, WorkItem
from gptme_runloops.pm_dispatch import is_direct_mention
from gptme_runloops.prompt_templates import PromptContext, render_instruction
from gptme_runloops.worker_records import (
    RateLimitRejection,
    fallback_outcome,
    parse_rate_limit_rejection,
    update_record_pr_state,
    write_fallback_session_record,
    write_post_session_record,
    write_worker_result_manifest,
)

DEFAULT_TIMEOUT = 900
ASSIGNED_ISSUE_TIMEOUT = 1500
GREPTILE_TIMEOUT = 2700


@dataclass(frozen=True)
class RunItem:
    """A grouped JSONL item emitted by the project-monitoring gate."""

    repo: str
    number: int | str
    title: str
    detail: str
    types: tuple[str, ...]
    all_numbers: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_grouped_json(cls, line: str) -> RunItem:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("grouped item must be a JSON object")
        repo, number = value.get("repo"), value.get("number")
        if not isinstance(repo, str) or not repo or number is None:
            raise ValueError("grouped item requires non-empty repo and number")
        source_types = value.get("types", [value.get("type")])
        if not isinstance(source_types, list):
            raise ValueError("grouped item types must be an array")
        types = tuple(v for v in source_types if isinstance(v, str) and v)
        if not types:
            raise ValueError("grouped item requires at least one type")
        source_numbers = value.get("all_numbers", [number])
        if not isinstance(source_numbers, list):
            raise ValueError("grouped item all_numbers must be an array")
        return cls(
            repo=repo,
            number=number,
            title=str(value.get("title", "")),
            detail=str(value.get("detail", "")),
            types=types,
            all_numbers=tuple(str(v) for v in source_numbers),
            raw=dict(value),
        )

    def to_merge_lifecycle_item(self) -> WorkItem:
        return WorkItem(repo=self.repo, number=self.number, types=self.types)


@dataclass(frozen=True)
class RunItemConfig:
    """Scalar policy passed from the slot unit or CLI."""

    workspace: Path
    backend: str
    model: str | None = None
    lane: str = "mixed"
    dispatch_id: str | None = None
    author: str = ""
    agent_name: str = "Agent"
    records_dir: Path | None = None
    run_salt: str = ""
    claim_mode: str = "acquire"

    def resolved_records_dir(self) -> Path:
        return self.records_dir or self.workspace / "state" / "session-records"


@dataclass(frozen=True)
class RunItemHooks:
    """Brain-side collaborators, injectable so package tests stay local."""

    runner: Sequence[str]
    monitoring_rules_file: Path | None = None
    sysprompt_file: Path | None = None
    claim: Callable[[str], bool] | None = None
    abandon: Callable[[str], None] | None = None
    trajectory_lines: Callable[[Path], Iterable[str]] | None = None
    rate_limit_block: Callable[[RateLimitRejection], None] | None = None
    prepare_trajectory_snapshot: Callable[[str, str, Path, Path], None] | None = None
    resolve_trajectory: Callable[[str, str, int, Path, Path], Path | None] | None = None

    @classmethod
    def from_workspace(cls, workspace: Path) -> RunItemHooks:
        return cls(
            runner=(str(workspace / "run.sh"),),
            monitoring_rules_file=workspace / "scripts/runs/github/monitoring-rules.md",
        )


@dataclass(frozen=True)
class ExecutionPlan:
    """All decisions taken before any hook/subprocess side effect."""

    repo: str
    number: int | str
    types: tuple[str, ...]
    timeout_seconds: int
    timeout_reason: str
    session_id: str
    record_file: str
    claim_key: str
    prompt: str
    backend: str
    model: str | None
    lane: str
    dispatch_id: str | None
    direct_mention: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class RunItemOutcome:
    item: RunItem
    plan: ExecutionPlan
    exit_code: int
    duration_seconds: int
    skipped_claimed: bool = False
    rate_limited: bool = False
    trajectory_path: Path | None = None


def _timeout_for(item: RunItem) -> tuple[int, str]:
    if "assigned_issue" in item.types:
        return ASSIGNED_ISSUE_TIMEOUT, "assigned_issue"
    if "pr_update" in item.types and any(
        typ in item.types
        for typ in ("greptile_needs_fix", "greptile_needs_improvement")
    ):
        return GREPTILE_TIMEOUT, "pr_update_with_greptile"
    return DEFAULT_TIMEOUT, "default"


def _item_slug(item: RunItem, index: int) -> str:
    return (
        f"{item.repo}_{item.number}_{index}".replace("/", "-")
        .replace("#", "-")
        .replace(" ", "-")
        .replace(":", "-")
    )


def _investigate(item: RunItem, workspace: Path) -> str:
    """Render the portable portion of the existing per-type investigate text."""
    sections: list[str] = []
    ctx = PromptContext(repo=item.repo, number=item.number, workspace=str(workspace))
    for kind in item.types:
        if kind == "pr_update":
            sections.append(
                "### PR Review & Comments\n```bash\n"
                f"gh pr view {item.number} --repo {item.repo}\n"
                f"gh pr view {item.number} --repo {item.repo} --comments\n"
                f"gh pr checks {item.number} --repo {item.repo}\n"
                f"gh pr view {item.number} --repo {item.repo} --json mergeable,mergeStateStatus\n"
                "```\n\nCheck every human and bot comment; never ignore a human comment in favor of bot review work."
            )
        elif kind == "ci_failure":
            sections.append(
                "### CI Failure Investigation\n```bash\n"
                f"gh pr checks {item.number} --repo {item.repo}\n"
                f"gh pr checks {item.number} --repo {item.repo} --json name,state,link --jq '.[] | select(.state == \"FAILURE\")'\n```"
            )
        elif kind == "assigned_issue":
            sections.append(
                "### Issue Details\n```bash\n"
                f"gh issue view {item.number} --repo {item.repo}\n"
                f"gh issue view {item.number} --repo {item.repo} --comments\n```\n\n"
                "Close the loop: do the work, create/update a local task if it cannot finish here, and reply with the concrete outcome."
            )
        elif kind == "master_ci_failure":
            commands = "\n".join(
                f"gh run view {run} --repo {item.repo} --log-failed | tail -60"
                for run in item.all_numbers
            )
            sections.append(f"### Master Branch CI Failure\n```bash\n{commands}\n```")
        elif kind == "merge_conflict":
            sections.append(
                "### Merge Conflict Resolution\n```bash\n"
                f"gh pr view {item.number} --repo {item.repo} --json mergeable,mergeStateStatus,headRefName\n```"
            )
        elif kind == "greptile_needs_fix":
            sections.append(render_instruction(InstructionKind.GREPTILE_NEEDS_FIX, ctx))
        elif kind == "greptile_needs_improvement":
            sections.append(
                render_instruction(InstructionKind.GREPTILE_NEEDS_IMPROVEMENT, ctx)
            )
    return (
        "\n".join(sections)
        or "Investigate the work item and act only when evidence warrants it."
    )


def _direct_mention_constraint(item: RunItem) -> str:
    if not is_direct_mention(item.detail):
        return ""
    return "\n## Required: Produce a Deliverable (Direct @Mention)\n\nThis is a direct @mention from Erik. Produce the requested deliverable or reply with the concrete blocker; a silent NOOP is not acceptable.\n"


def plan_run_item(
    item: RunItem,
    config: RunItemConfig,
    *,
    index: int = 1,
    monitoring_rules: str = "",
) -> ExecutionPlan:
    """Build a deterministic plan without reading files or invoking hooks."""
    timeout, reason = _timeout_for(item)
    slug = _item_slug(item, index)
    session_id = str(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"monitor-{slug}-{config.run_salt or 'default'}")
    )
    prompt = (
        f"You are {config.agent_name}, running a focused project monitoring session. Your identity files have been injected as system context.\n\n"
        "## Your Task\n\nInvestigate and act on this work item:\n\n"
        f"- **Event(s)**: {item.types[0]}\n- **Repo**: {item.repo}\n- **Number**: #{item.number}\n- **Title**: {item.title}\n- **Detail**: {item.detail}\n\n"
        f"Your GitHub author name is: {config.author}\n{_direct_mention_constraint(item)}\n"
        "## Step 1: Investigate (3 min)\n\nGet full context for this item. Read ALL sources — never truncate output.\n"
        f"{_investigate(item, config.workspace)}\n\n## Step 2: Classify & Execute\n\n{monitoring_rules}\n\n"
        "## Time Budget\n\n"
        f"You have ~{timeout // 60} minutes available for this item.\n"
        "- Treat the limit as a stall guard, not a rush order.\n"
        "- Keep naturally sequential work together: investigate -> fix -> verify -> reply.\n"
        "- If the item needs no action after investigation, just exit. No journal. No commit.\n"
    )
    return ExecutionPlan(
        repo=item.repo,
        number=item.number,
        types=item.types,
        timeout_seconds=timeout,
        timeout_reason=reason,
        session_id=session_id,
        record_file=str(config.resolved_records_dir() / f"{slug}.json"),
        claim_key=f"github:{item.repo}#{item.number}",
        prompt=prompt,
        backend=config.backend,
        model=config.model,
        lane=config.lane,
        dispatch_id=config.dispatch_id,
        direct_mention=is_direct_mention(item.detail),
    )


def _runner_command(plan: ExecutionPlan, hooks: RunItemHooks) -> list[str]:
    command = [
        *hooks.runner,
        "--backend",
        plan.backend,
        "--no-lock",
        "--no-pull",
        "--no-grade",
    ]
    if hooks.sysprompt_file is not None:
        command.extend(("--sysprompt-file", str(hooks.sysprompt_file)))
    command.extend(("--timeout", str(plan.timeout_seconds)))
    if plan.model:
        command.extend(("--model", plan.model))
    return [*command, plan.prompt]


def _read_ref_path(ref_path: Path) -> Path | None:
    try:
        value = ref_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _newest_file(paths: Iterable[Path], *, min_mtime: int | None = None) -> Path | None:
    newest: Path | None = None
    newest_mtime = -1
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        mtime = int(stat.st_mtime)
        if min_mtime is not None and mtime < min_mtime:
            continue
        if newest is None or mtime > newest_mtime:
            newest = path
            newest_mtime = mtime
    return newest


def prepare_monitoring_trajectory_snapshot(
    backend: str,
    session_id: str,
    home: Path,
    tmp_dir: Path = Path("/tmp"),
) -> None:
    """Capture pre-run trajectory listings for backends without session refs."""
    if backend == "copilot-cli":
        state_dir = home / ".copilot" / "session-state"
        if state_dir.is_dir():
            snapshot = tmp_dir / f"copilot-pre-snapshot-{session_id}.txt"
            snapshot.write_text(
                "\n".join(sorted(child.name for child in state_dir.iterdir())) + "\n",
                encoding="utf-8",
            )
    elif backend == "codex":
        sessions_dir = home / ".codex" / "sessions"
        if sessions_dir.is_dir():
            snapshot = tmp_dir / f"codex-pre-snapshot-{session_id}.txt"
            snapshot.write_text(
                "\n".join(
                    sorted(str(path) for path in sessions_dir.rglob("rollout-*.jsonl"))
                )
                + "\n",
                encoding="utf-8",
            )


def resolve_monitoring_trajectory(
    backend: str,
    session_id: str,
    started_epoch: int,
    home: Path,
    tmp_dir: Path = Path("/tmp"),
) -> Path | None:
    """Resolve the trajectory file produced by ``run.sh`` for one PM item.

    This ports worker.sh's backend-specific post-run discovery.  It deliberately
    uses session-specific ref files for stream-json backends and pre/post
    snapshot files for copilot/codex so concurrent workers do not race on mtimes.
    """
    if backend == "claude-code" and session_id:
        ref = tmp_dir / f"cc-session-log-ref-{session_id}.txt"
        candidate = _read_ref_path(ref)
        if candidate is not None and _file_size(candidate) > 5000:
            return candidate

    if backend == "grok-build" and session_id:
        ref = tmp_dir / f"grok-build-session-log-ref-{session_id}.txt"
        candidate = _read_ref_path(ref)
        try:
            ref.unlink()
        except OSError:
            pass
        if candidate is not None and _file_size(candidate) > 1000:
            return candidate

    if backend == "copilot-cli":
        pre_snapshot = tmp_dir / f"copilot-pre-snapshot-{session_id}.txt"
        if pre_snapshot.is_file():
            state_dir = home / ".copilot" / "session-state"
            before = set(pre_snapshot.read_text(encoding="utf-8").splitlines())
            candidates = []
            if state_dir.is_dir():
                for child in state_dir.iterdir():
                    if child.name not in before:
                        candidates.append(child / "events.jsonl")
            try:
                pre_snapshot.unlink()
            except OSError:
                pass
            return _newest_file(candidates, min_mtime=started_epoch)

    if backend == "codex":
        pre_snapshot = tmp_dir / f"codex-pre-snapshot-{session_id}.txt"
        if pre_snapshot.is_file():
            sessions_dir = home / ".codex" / "sessions"
            before = set(pre_snapshot.read_text(encoding="utf-8").splitlines())
            candidates = []
            if sessions_dir.is_dir():
                for candidate in sessions_dir.rglob("rollout-*.jsonl"):
                    if str(candidate) not in before:
                        candidates.append(candidate)
            try:
                pre_snapshot.unlink()
            except OSError:
                pass
            return _newest_file(candidates)

    return None


def write_claude_rate_limit_block(
    rejection: RateLimitRejection,
    quota_dir: Path,
    *,
    credential_target: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write the per-sub Claude Code quota block file for a confirmed rejection.

    Mirrors worker.sh:159-185: only call this after
    :func:`parse_rate_limit_rejection` confirms ``status == rejected``.  The
    ``seven_day_sonnet`` cap gets a sonnet-specific block file; other caps use
    the generic per-sub block.  Missing/zero reset timestamps block for 6h.
    """
    quota_dir.mkdir(parents=True, exist_ok=True)
    suffix = ""
    if credential_target:
        marker = ".credentials.json."
        if marker in credential_target:
            suffix = credential_target.rsplit(marker, 1)[1] + "-"
    rate_type = str(rejection.rate_limit_type or "")
    if rate_type == "seven_day_sonnet":
        block_path = quota_dir / f"claude-code-{suffix}sonnet-rate-limited-until.txt"
    else:
        block_path = quota_dir / f"claude-code-{suffix}rate-limited-until.txt"

    reset = str(rejection.resets_at or "")
    if reset and reset != "0":
        until = datetime.fromtimestamp(int(reset), tz=timezone.utc)
    else:
        until = (now or datetime.now(timezone.utc)) + timedelta(hours=6)
    block_path.write_text(until.isoformat(), encoding="utf-8")
    return block_path


def execute_plan(
    plan: ExecutionPlan, item: RunItem, hooks: RunItemHooks
) -> RunItemOutcome:
    """Acquire a claim, invoke ``run.sh``, and guard blocks on real rejection."""
    if hooks.claim is not None and not hooks.claim(plan.claim_key):
        return RunItemOutcome(item, plan, 0, 0, skipped_claimed=True)
    started = time.monotonic()
    started_epoch = int(time.time())
    old_handler = signal.getsignal(signal.SIGTERM)

    def _terminate(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, _terminate)
    try:
        env = os.environ.copy()
        if plan.backend == "claude-code":
            env["CC_SESSION_ID"] = plan.session_id
        elif plan.backend == "grok-build":
            env["GROK_BUILD_SESSION_ID"] = plan.session_id
        if hooks.prepare_trajectory_snapshot is not None:
            hooks.prepare_trajectory_snapshot(
                plan.backend,
                plan.session_id,
                Path.home(),
                Path("/tmp"),
            )
        completed = subprocess.run(_runner_command(plan, hooks), env=env, check=False)
        trajectory_path = None
        if hooks.resolve_trajectory is not None:
            trajectory_path = hooks.resolve_trajectory(
                plan.backend,
                plan.session_id,
                started_epoch,
                Path.home(),
                Path("/tmp"),
            )
        rate_limited = False
        if (
            completed.returncode
            and plan.backend == "claude-code"
            and trajectory_path is not None
            and hooks.trajectory_lines
            and hooks.rate_limit_block
        ):
            rejection = parse_rate_limit_rejection(
                hooks.trajectory_lines(trajectory_path or Path(plan.record_file))
            )
            if rejection is not None:
                hooks.rate_limit_block(rejection)
                rate_limited = True
        return RunItemOutcome(
            item,
            plan,
            completed.returncode,
            int(time.monotonic() - started),
            rate_limited=rate_limited,
            trajectory_path=trajectory_path,
        )
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        if hooks.abandon is not None:
            hooks.abandon(plan.claim_key)


@dataclass(frozen=True)
class RunPostSessionHooks:
    """Injectable bookkeeping callables for :func:`run_post_session`.

    All fields are ``None`` by default; a ``None`` hook skips that step,
    matching the bash ``|| true`` tolerance in worker.sh.
    """

    # Primary record writer: gptme_sessions.post_session.post_session
    post_session: Callable[..., Any] | None = None
    # SessionStore factory: lambda path: SessionStore(sessions_dir=path)
    make_store: Callable[[Path], Any] | None = None
    # Fallback record factory: lambda **kw: SessionRecord(**kw).to_dict()
    make_record: Callable[..., Mapping[str, Any]] | None = None
    # metaproductivity.pr_outcome.upgrade_outcome_from_pr_state (optional)
    upgrade_outcome: Callable[[dict[str, Any]], Any] | None = None
    # agent_events.worker_results.build_worker_result
    build_worker_result: Callable[..., dict[str, Any]] | None = None
    # agent_events.worker_results.write_worker_result
    write_worker_result: Callable[[Path, Mapping[str, Any]], Any] | None = None
    # agent_events.worker_results.load_worker_result
    load_worker_result: Callable[[Path], Mapping[str, Any] | None] | None = None


def run_post_session(
    outcome: RunItemOutcome,
    hooks: RunPostSessionHooks,
    *,
    workspace: Path,
    pr_state_before_json: str = "",
) -> None:
    """Write post-session bookkeeping records for a completed plan execution.

    Mirrors worker.sh:283-627. Each step is guarded by ``|| true``-equivalent
    exception swallowing: a failure in one step does not abort subsequent steps.
    Skipped-claim outcomes write no record (same as the bash guard at :278).

    Call this after :func:`execute_plan` returns, even on non-zero exits —
    the record is the primary artifact that session quality analysis needs.

    ``pr_state_before_json`` should be the raw JSON from a
    ``gh pr view --json state,headRefOid,mergeCommit`` call taken *before*
    :func:`execute_plan`; an empty string silently skips the before-state
    fields (worker.sh parity when ``_before_json`` was not captured).
    """
    if outcome.skipped_claimed:
        return

    plan = outcome.plan
    item = outcome.item
    record_file = Path(plan.record_file)
    trajectory_path_str = (
        str(outcome.trajectory_path) if outcome.trajectory_path else None
    )

    # Step 1: Primary record via gptme_sessions (worker.sh:283-323).
    primary_ok = False
    if hooks.post_session is not None and hooks.make_store is not None:
        try:
            write_post_session_record(
                record_file,
                harness=plan.backend,
                model=plan.model,
                session_id=plan.session_id,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
                item_timeout=plan.timeout_seconds,
                trajectory_path=trajectory_path_str,
                post_session=hooks.post_session,
                make_store=hooks.make_store,
            )
            primary_ok = True
        except Exception:
            pass

    # Step 2: Fallback via metaproductivity.sessions (worker.sh:325-373).
    if not primary_ok and hooks.make_record is not None:
        try:
            write_fallback_session_record(
                record_file,
                harness=plan.backend,
                model=plan.model,
                outcome=fallback_outcome(outcome.exit_code),
                session_id=plan.session_id,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
                item_timeout=plan.timeout_seconds,
                trajectory_path=trajectory_path_str,
                make_record=hooks.make_record,
            )
        except Exception:
            pass

    # Step 3: PR-state before/after diff (worker.sh:377-502).
    # Raises for non-numeric PR numbers (same as heredoc dying) → swallowed.
    try:
        update_record_pr_state(
            record_file,
            repo=item.repo,
            number=item.number,
            before_json=pr_state_before_json,
            cwd=workspace,
            upgrade_outcome=hooks.upgrade_outcome,
        )
    except Exception:
        pass

    # Step 4: Worker-result manifest (worker.sh:505-627).
    if (
        hooks.build_worker_result is not None
        and hooks.write_worker_result is not None
        and hooks.load_worker_result is not None
    ):
        try:
            write_worker_result_manifest(
                record_file,
                repo=item.repo,
                number=item.number,
                session_id=plan.session_id,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
                model=plan.model,
                item_types=item.types,
                build_worker_result=hooks.build_worker_result,
                write_worker_result=hooks.write_worker_result,
                load_worker_result=hooks.load_worker_result,
            )
        except Exception:
            pass


def load_items(work_file: Path) -> list[RunItem]:
    """Load valid grouped items, skipping malformed gate lines like bash does."""
    if not work_file.is_file():
        raise FileNotFoundError(work_file)
    items: list[RunItem] = []
    for line in work_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(RunItem.from_grouped_json(line))
        except (ValueError, json.JSONDecodeError):
            continue
    if not items:
        raise ValueError("work file contains no valid grouped items")
    return items
