#!/usr/bin/env python3
"""Checker pattern implementation for task verification.

The checker pattern provides periodic verification of task state,
detecting completion, identifying issues, and auto-creating fix tasks.

This pattern is inspired by Claude Code's checker agent which monitors
task progress and ensures quality.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypedDict

from .utils import (
    TaskInfo,
    load_tasks,
)

logger = logging.getLogger(__name__)


class CheckDetails(TypedDict, total=False):
    """Type for check details dictionary."""

    completed: int
    total: int
    percentage: float
    resolved: list[str]
    unresolved: list[str]
    valid_states: list[str]
    previous_state: str
    allowed_transitions: list[str]
    next_states: list[str]


class CheckResultDict(TypedDict):
    """Type for individual check result."""

    check: str
    passed: bool
    message: str
    details: CheckDetails


@dataclass
class CheckResult:
    """Result of a task check."""

    task_id: str
    timestamp: str
    status: str  # "passed", "failed", "needs_attention", "in_progress"
    checks: list[CheckResultDict] = field(default_factory=list)
    fixes_created: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class CheckerConfig:
    """Configuration for the checker agent."""

    # Polling configuration
    poll_interval_seconds: int = 30  # How often to check (default: 30s)
    max_polls: int = 100  # Maximum number of polls before timeout

    # Verification settings
    verify_subtasks: bool = True  # Check subtask completion
    verify_dependencies: bool = True  # Check dependency resolution
    verify_state_transitions: bool = True  # Check valid state transitions

    # Auto-fix settings
    auto_create_fix_tasks: bool = False  # Auto-create tasks for issues
    fix_task_prefix: str = "fix-"  # Prefix for auto-created fix tasks

    # Agent settings
    use_agent_verification: bool = False  # Use LLM agent for semantic verification
    agent_model: str | None = None  # Model for agent verification


# Valid state transitions
VALID_TRANSITIONS: dict[str, list[str]] = {
    "backlog": ["todo", "cancelled"],
    "todo": ["active", "backlog", "cancelled"],
    "active": ["ready_for_review", "waiting", "done", "cancelled"],
    "ready_for_review": ["active", "done", "cancelled"],  # Can go back to active if review fails
    "waiting": ["active", "cancelled"],
    "done": [],  # Terminal state
    "cancelled": [],  # Terminal state
}


def check_subtask_completion(task: TaskInfo) -> CheckResultDict:
    """Check if all subtasks are completed for a task marked as done/ready_for_review."""
    details: CheckDetails = {}
    result: CheckResultDict = {
        "check": "subtask_completion",
        "passed": True,
        "message": "",
        "details": details,
    }

    if task.subtasks.total == 0:
        result["message"] = "No subtasks defined"
        return result

    completion_pct = (task.subtasks.completed / task.subtasks.total) * 100
    details["completed"] = task.subtasks.completed
    details["total"] = task.subtasks.total
    details["percentage"] = completion_pct

    if task.state in ["done", "ready_for_review"] and completion_pct < 100:
        result["passed"] = False
        result["message"] = (
            f"Task marked as {task.state} but only {task.subtasks.completed}/{task.subtasks.total} "
            f"subtasks completed ({completion_pct:.0f}%)"
        )
    else:
        result["message"] = f"{task.subtasks.completed}/{task.subtasks.total} subtasks completed"

    return result


def check_dependency_resolution(task: TaskInfo, all_tasks: dict[str, TaskInfo]) -> CheckResultDict:
    """Check if all dependencies are resolved for an active/done task."""
    details: CheckDetails = {}
    result: CheckResultDict = {
        "check": "dependency_resolution",
        "passed": True,
        "message": "",
        "details": details,
    }

    if not task.requires:
        result["message"] = "No dependencies"
        return result

    unresolved: list[str] = []
    resolved: list[str] = []

    for dep in task.requires:
        if dep.startswith(("http://", "https://")):
            # URL dependency - can't check without cache
            continue
        dep_task = all_tasks.get(dep)
        if dep_task is None:
            unresolved.append(f"{dep} (not found)")
        elif dep_task.state not in ["done", "cancelled"]:
            unresolved.append(f"{dep} ({dep_task.state})")
        else:
            resolved.append(dep)

    # Exclude URL deps from total since they're skipped
    url_deps = sum(1 for dep in task.requires if dep.startswith(("http://", "https://")))
    details["resolved"] = resolved
    details["unresolved"] = unresolved
    details["total"] = len(task.requires) - url_deps

    if unresolved and task.state in ["active", "done", "ready_for_review"]:
        result["passed"] = False
        result["message"] = f"Unresolved dependencies: {', '.join(unresolved)}"
    else:
        result["message"] = f"{len(resolved)}/{len(task.requires)} dependencies resolved"

    return result


def check_state_validity(task: TaskInfo, previous_state: str | None = None) -> CheckResultDict:
    """Check if task state is valid and transition is allowed."""
    details: CheckDetails = {}
    result: CheckResultDict = {
        "check": "state_validity",
        "passed": True,
        "message": "",
        "details": details,
    }

    valid_states = list(VALID_TRANSITIONS.keys())

    if task.state not in valid_states:
        result["passed"] = False
        result["message"] = f"Invalid state: {task.state}"
        details["valid_states"] = valid_states
        return result

    if previous_state and previous_state in VALID_TRANSITIONS:
        allowed = VALID_TRANSITIONS[previous_state]
        if task.state not in allowed and task.state != previous_state:
            result["passed"] = False
            result["message"] = (
                f"Invalid state transition: {previous_state} → {task.state}. " f"Allowed: {allowed}"
            )
            details["previous_state"] = previous_state
            details["allowed_transitions"] = allowed
            return result

    result["message"] = f"State '{task.state}' is valid"
    details["next_states"] = VALID_TRANSITIONS.get(task.state, [])
    return result


def run_checker(
    task_id: str,
    repo_root: Path,
    config: CheckerConfig | None = None,
    on_check: Callable[[CheckResult], None] | None = None,
) -> CheckResult:
    """Run checker verification on a task.

    Args:
        task_id: Task ID or name to check
        repo_root: Repository root path
        config: Checker configuration (uses defaults if None)
        on_check: Optional callback for each check iteration

    Returns:
        CheckResult with verification outcome
    """
    config = config or CheckerConfig()
    tasks_dir = repo_root / "tasks"

    # Load tasks
    tasks = load_tasks(tasks_dir)
    all_tasks = {t.name: t for t in tasks}

    # Find target task
    task = all_tasks.get(task_id)
    if not task:
        # Try matching by ID
        for t in tasks:
            if t.id == task_id:
                task = t
                break

    if not task:
        return CheckResult(
            task_id=task_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="failed",
            message=f"Task not found: {task_id}",
        )

    # Run checks
    checks: list[CheckResultDict] = []
    all_passed = True

    # Check 1: Subtask completion
    if config.verify_subtasks:
        subtask_check = check_subtask_completion(task)
        checks.append(subtask_check)
        if not subtask_check["passed"]:
            all_passed = False

    # Check 2: Dependency resolution
    if config.verify_dependencies:
        dep_check = check_dependency_resolution(task, all_tasks)
        checks.append(dep_check)
        if not dep_check["passed"]:
            all_passed = False

    # Check 3: State validity
    if config.verify_state_transitions:
        state_check = check_state_validity(task)
        checks.append(state_check)
        if not state_check["passed"]:
            all_passed = False

    # Determine overall status
    if all_passed:
        if task.state in ["done", "cancelled"]:
            status = "passed"
            message = f"Task {task_id} verification passed (state: {task.state})"
        elif task.state == "ready_for_review":
            status = "needs_attention"
            message = f"Task {task_id} is ready for review"
        else:
            status = "in_progress"
            message = f"Task {task_id} is in progress (state: {task.state})"
    else:
        status = "failed"
        failed_checks = [c["check"] for c in checks if not c["passed"]]
        message = f"Task {task_id} failed checks: {', '.join(failed_checks)}"

    result = CheckResult(
        task_id=task_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        checks=checks,
        message=message,
    )

    if on_check:
        on_check(result)

    return result


def poll_task_completion(
    task_id: str,
    repo_root: Path,
    config: CheckerConfig | None = None,
    on_poll: Callable[[int, CheckResult], bool] | None = None,
) -> CheckResult:
    """Poll a task until it reaches completion or timeout.

    Args:
        task_id: Task ID to poll
        repo_root: Repository root path
        config: Checker configuration
        on_poll: Callback(poll_num, result) - return False to stop polling

    Returns:
        Final CheckResult
    """
    config = config or CheckerConfig()

    for poll_num in range(config.max_polls):
        result = run_checker(task_id, repo_root, config)

        # Check if we should stop
        if result.status in ["passed", "failed"]:
            return result

        # Callback
        if on_poll:
            should_continue = on_poll(poll_num, result)
            if not should_continue:
                return result

        # Wait before next poll
        if poll_num < config.max_polls - 1:
            time.sleep(config.poll_interval_seconds)

    # Timeout
    return CheckResult(
        task_id=task_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        status="failed",
        message=f"Timeout after {config.max_polls} polls",
    )
