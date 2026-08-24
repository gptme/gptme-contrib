#!/usr/bin/env -S uv run python3
"""List tasks ready for work while respecting dependencies and external blockers.

A generic task filtering tool for any agent running concurrent sessions.
Wraps gptodo's readiness logic and optionally filters out coordination-claimed tasks.

For agents in Bob's workspace or with the coordination package available,
``--skip-claim-held`` automatically enables foreign cascade-claim filtering.
Other agents get the core waiting_for/depends filtering without coordination.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Optional workspace tooling — gracefully degrade if unavailable
_HAS_COORDINATION = False
_HAS_METAPRODUCTIVITY = False

try:
    GPTODO_SRC = REPO_ROOT / "packages" / "gptodo" / "src"
    if GPTODO_SRC.is_dir():
        gptodo_src_str = str(GPTODO_SRC)
        if gptodo_src_str not in sys.path:
            sys.path.insert(0, gptodo_src_str)
    from gptodo.utils import (
        find_repo_root,
        is_task_ready,
        load_tasks,
        task_has_waiting_blocker,
        task_is_waiting_for_date,
        task_to_dict,
    )
except (ImportError, ModuleNotFoundError) as e:
    print(f"ERROR: gptodo is required but not found: {e}", file=sys.stderr)
    sys.exit(1)

try:
    COORDINATION_SRC = REPO_ROOT / "packages" / "coordination" / "src"
    if COORDINATION_SRC.is_dir():
        coordination_src_str = str(COORDINATION_SRC)
        if coordination_src_str not in sys.path:
            sys.path.insert(0, coordination_src_str)
    from coordination.cascade_tasks import completed_cascade_claim_task_reopened
    from coordination.db import resolve_coordination_db_path
    from coordination.health import preselect_holder_pid_is_dead

    _HAS_COORDINATION = True
except (ImportError, ModuleNotFoundError):
    pass

try:
    METAPRODUCTIVITY_SRC = REPO_ROOT / "packages" / "metaproductivity" / "src"
    if METAPRODUCTIVITY_SRC.is_dir():
        metaproductivity_src_str = str(METAPRODUCTIVITY_SRC)
        if metaproductivity_src_str not in sys.path:
            sys.path.insert(0, metaproductivity_src_str)
    _HAS_METAPRODUCTIVITY = True
except (ImportError, ModuleNotFoundError):
    pass

STATE_CHOICES = (
    "backlog",
    "todo",
    "active",
    "ready_for_review",
    "waiting",
    "someday",
    "both",
    "actionable",
)


def filter_by_state(tasks: list[Any], state: str) -> list[Any]:
    """Filter tasks by state."""
    if state == "backlog":
        return [task for task in tasks if task.state == "backlog"]
    if state == "todo":
        return [task for task in tasks if task.state == "todo"]
    if state == "active":
        return [task for task in tasks if task.state == "active"]
    if state == "ready_for_review":
        return [task for task in tasks if task.state == "ready_for_review"]
    if state == "waiting":
        return [task for task in tasks if task.state == "waiting"]
    if state == "someday":
        return [task for task in tasks if task.state == "someday"]
    if state == "actionable":
        return [
            task
            for task in tasks
            if task.state
            in ["backlog", "todo", "active", "ready_for_review", "waiting"]
        ]
    # Default: "both" = backlog + todo + active
    return [task for task in tasks if task.state in ["backlog", "todo", "active"]]


def select_ready_tasks(
    filtered_tasks: list[Any],
    tasks_dict: dict[str, Any],
    state: str,
    issue_cache: dict[str, Any] | None = None,
) -> list[Any]:
    """Select truly actionable tasks from the filtered set.

    Filters by waiting_for, unresolved dependencies, and state-specific criteria.
    """
    if state == "someday":
        # Return someday tasks that would be dependency-ready if flipped to backlog
        from dataclasses import replace

        return [
            task
            for task in filtered_tasks
            if not task_has_waiting_blocker(task)
            if not task_is_waiting_for_date(task)
            if bool(
                is_task_ready(replace(task, state="backlog"), tasks_dict, issue_cache)
            )
        ]
    if state == "ready_for_review":
        return [task for task in filtered_tasks if not task_has_waiting_blocker(task)]
    if state == "waiting":
        # Return waiting tasks whose time gates have elapsed and have no remaining blocker.
        # A task with waiting_for still set is blocked on a human/external condition even
        # if its date gate passed — exclude it so agents don't pick up stalled work.
        # A task with a probe field still needs probe resolution before it is actionable.
        return [
            task
            for task in filtered_tasks
            if task.state == "waiting"
            if task.wait is not None
            if not task_is_waiting_for_date(task)
            if not task.metadata.get("waiting_for")
            if not task.metadata.get("probe")
        ]
    if state == "actionable":
        from dataclasses import replace

        return [
            task
            for task in filtered_tasks
            if (task.state == "ready_for_review" and not task_has_waiting_blocker(task))
            or (
                task.state == "waiting"
                and task.wait is not None
                and not task_is_waiting_for_date(task)
                and not task.metadata.get("waiting_for")
                and not task.metadata.get("probe")
            )
            or (
                task.state in ["backlog", "todo", "active"]
                and is_task_ready(task, tasks_dict, issue_cache)
            )
        ]
    # Default: backlog, todo, active + dependency-ready
    return [
        task for task in filtered_tasks if is_task_ready(task, tasks_dict, issue_cache)
    ]


def get_foreign_cascade_claims(repo_root: Path) -> dict[str, str]:
    """Return foreign cascade-claim task IDs (if coordination is available).

    Returns a dict mapping task_id -> holder for foreign cascade:task:* claims.
    If coordination is unavailable, returns an empty dict.
    """
    if not _HAS_COORDINATION:
        return {}

    import sqlite3

    db_path = resolve_coordination_db_path(repo_root=repo_root)
    my_agent = (
        os.environ.get("READY_TASKS_COORDINATION_AGENT")
        or os.environ.get("CASCADE_COORDINATION_AGENT")
        or os.environ.get("AGENT_ID")
        or ""
    ).strip()

    if not db_path.exists():
        return {}

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='work'"
            ).fetchone()
            if not table_check:
                return {}

            rows = conn.execute(
                """SELECT task_id, claimer, status, expires_at FROM work
                WHERE task_id LIKE 'cascade:task:%'
                  AND status IN ('claimed', 'completed')
                  AND expires_at >= datetime('now')
                  AND (? = '' OR claimer != ?)""",
                (my_agent, my_agent),
            ).fetchall()
    except Exception as exc:
        print(
            f"warning: coordination claim filtering disabled due to error: {exc}",
            file=sys.stderr,
        )
        return {}

    prefix = "cascade:task:"
    blocked: dict[str, str] = {}
    for row in rows:
        task_id = str(row["task_id"])
        status = str(row["status"])
        claimer = str(row["claimer"] or "unknown")
        if status == "claimed" and claimer != "unknown":
            try:
                if preselect_holder_pid_is_dead(claimer):
                    continue
            except Exception:
                pass  # conservatively treat as live (keep claim in blocked set)
        if status == "completed" and completed_cascade_claim_task_reopened(
            task_id,
            repo_root=repo_root,
        ):
            continue
        stripped_task_id = (
            task_id[len(prefix) :] if task_id.startswith(prefix) else task_id
        )
        blocked[stripped_task_id] = claimer
    return blocked


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List ready tasks with dependency filtering"
    )
    parser.add_argument(
        "--state",
        choices=STATE_CHOICES,
        default="both",
        help="Filter by task state (default: both = backlog + todo + active)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON object with metadata",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Output as JSONL (one task per line)",
    )
    parser.add_argument(
        "--skip-claim-held",
        action="store_true",
        help="Filter out tasks held by foreign coordination claims (coordination package required)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load tasks
    load_errors: list[str] = []
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        all_tasks = load_tasks(tasks_dir, errors_out=load_errors)
    finally:
        logging.disable(previous_logging_disable)

    if load_errors:
        print(
            f"warning: {len(load_errors)} task file(s) failed to load: {load_errors}",
            file=sys.stderr,
        )

    tasks_dict = {task.name: task for task in all_tasks}

    # Filter by state
    filtered_tasks = filter_by_state(all_tasks, args.state)

    # Select ready tasks
    ready_tasks = select_ready_tasks(
        filtered_tasks, tasks_dict, args.state, issue_cache=None
    )

    # Optionally filter by coordination claims
    foreign_claims: dict[str, str] = {}
    if args.skip_claim_held:
        if not _HAS_COORDINATION:
            print(
                "WARNING: --skip-claim-held requires coordination package, skipping claim filter",
                file=sys.stderr,
            )
        else:
            foreign_claims = get_foreign_cascade_claims(repo_root)
            ready_tasks = [
                task for task in ready_tasks if task.name not in foreign_claims
            ]

    # Sort by priority and creation time
    ready_tasks.sort(key=lambda t: (-t.priority_rank, t.created))

    # Output
    if args.jsonl:
        for task in ready_tasks:
            print(json.dumps(task_to_dict(task)))
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "ready_tasks": [task_to_dict(task) for task in ready_tasks],
                    "count": len(ready_tasks),
                    "state": args.state,
                    "state_matching_count": len(filtered_tasks),
                },
                indent=2,
            )
        )
        return 0

    # Default text output
    if not ready_tasks:
        print(f"Ready tasks: 0 (state={args.state}, matching={len(filtered_tasks)})")
        if foreign_claims:
            print(f"Claim-blocked: {len(foreign_claims)}")
        return 0

    print(f"Ready tasks: {len(ready_tasks)}")
    for task in ready_tasks:
        priority = task.priority or "-"
        print(f"- {task.name} [{task.state}] priority={priority}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
