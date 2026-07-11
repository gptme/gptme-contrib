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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gptme_runloops.merge_lifecycle import InstructionKind, WorkItem
from gptme_runloops.pm_dispatch import is_direct_mention
from gptme_runloops.prompt_templates import PromptContext, render_instruction
from gptme_runloops.worker_records import RateLimitRejection, parse_rate_limit_rejection

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


def execute_plan(
    plan: ExecutionPlan, item: RunItem, hooks: RunItemHooks
) -> RunItemOutcome:
    """Acquire a claim, invoke ``run.sh``, and guard blocks on real rejection."""
    if hooks.claim is not None and not hooks.claim(plan.claim_key):
        return RunItemOutcome(item, plan, 0, 0, skipped_claimed=True)
    started = time.monotonic()
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
        completed = subprocess.run(_runner_command(plan, hooks), env=env, check=False)
        rate_limited = False
        if completed.returncode and hooks.trajectory_lines and hooks.rate_limit_block:
            rejection = parse_rate_limit_rejection(
                hooks.trajectory_lines(Path(plan.record_file))
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
        )
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        if hooks.abandon is not None:
            hooks.abandon(plan.claim_key)


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
