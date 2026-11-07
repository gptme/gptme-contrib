"""Infer why a session was launched, and record it before the session runs.

Dispatch provenance answers "what started this session?" — a worker spawn, an
event, a scheduled slot, a task claim, or systemd. The object is
identifier-only: event payloads and dispatcher prompt text stay private to the
stores that already own them.

``post_session`` already reads the cause from ``AGENT_DISPATCH_CAUSE`` (legacy
alias ``BOB_DISPATCH_CAUSE``), so a dispatcher that exports this object gets
lineage with no further wiring. The start ledger written by ``--record-start``
survives a runner that dies before the session is recorded.

Neutral ``AGENT_*`` names are read first and legacy ``BOB_*`` names keep
working, following gptme/gptme-contrib#1724. An inherited value never overrides
a fresh launch: pass ``--refresh`` so a child re-infers from its own
environment instead of forwarding its parent's cause verbatim.

Usage::

    python -m gptme_sessions.dispatch_cause --workspace .
    python -m gptme_sessions.dispatch_cause --workspace . --refresh --record-start
    python -m gptme_sessions.dispatch_cause --workspace . --trigger
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "append_start",
    "infer_cause",
    "last_release",
    "session_trigger",
]

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?:\D|$)")

#: Dispatcher-specific item types that imply a PR target.
PR_TYPES = frozenset(
    {
        "pr_update",
        "merge_ready",
        "merge_conflict",
        "greptile_needs_fix",
        "greptile_needs_improvement",
        "reviewer_needs_fix",
        "reviewer_needs_improvement",
        "pr_changes_requested_stale",
        "ci_failure_pr",
        "review_requested",
    }
)

#: Neutral spelling first, then the legacy alias (gptme-contrib#1724).
ALIASES: dict[str, tuple[str, ...]] = {
    "cause": ("AGENT_DISPATCH_CAUSE", "BOB_DISPATCH_CAUSE"),
    "kind": ("AGENT_DISPATCH_KIND", "BOB_DISPATCH_KIND"),
    "id": ("AGENT_DISPATCH_ID", "BOB_DISPATCH_ID"),
    "parent": ("AGENT_PARENT_SESSION_ID", "BOB_PARENT_SESSION_ID"),
    "receipt": ("AGENT_RECEIPT_SESSION_ID", "BOB_RECEIPT_SESSION_ID"),
    "session": (
        "AGENT_SESSION_ID",
        "BOB_SESSION_ID",
        "CC_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "GROK_BUILD_SESSION_ID",
        "PI_SESSION_ID",
    ),
}

SPAWN_KINDS = frozenset({"fanout", "fanout-btb", "worker"})
DISPATCH_KINDS = frozenset({"event", "pm-slot", "agent-msg", "cascade-task"})


def _env(env: Mapping[str, str], key: str) -> str | None:
    """Return the first non-empty spelling for ``key``, neutral name first."""
    for name in ALIASES[key]:
        value = env.get(name)
        if value:
            return value
    return None


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pm_items(path: str) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        return [_object(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except (OSError, UnicodeError):
        return []


def _pr_ref(url: str) -> str | None:
    match = PR_URL.search(url)
    return f"{match[1]}#{match[2]}" if match else None


def _types(item: dict[str, Any]) -> list[str]:
    kinds = item.get("types")
    if not isinstance(kinds, list) or not kinds:
        kinds = [item.get("type")]
    return [kind for kind in kinds if isinstance(kind, str)]


def _item_pr(item: dict[str, Any]) -> str | None:
    explicit = _pr_ref(str(item.get("url") or ""))
    if explicit:
        return explicit
    repo, number = str(item.get("repo") or ""), str(item.get("number") or "")
    if (
        PR_TYPES.intersection(_types(item))
        and re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
        and number.isdecimal()
        and int(number) > 0
    ):
        return f"{repo}#{number}"
    return None


def infer_cause(env: Mapping[str, str]) -> dict[str, Any]:
    """Prefer the explicit launch source over incidental selection state."""
    intent = _object(env.get("CASCADE_INTENT", ""))
    task = intent.get("task_id")
    if env.get("CASCADE_COORDINATION_CLAIMED") == "1" and (
        env.get("CASCADE_COORDINATION_SOURCE") == "task"
        or env.get("CASCADE_COORDINATION_KEY", "").startswith("cascade:task:")
    ):
        task = env.get("CASCADE_SELECTED_ID") or task
    if not isinstance(task, str) or not task.strip():
        task = None

    kind = _env(env, "kind")
    cause: dict[str, Any] = {
        "kind": "unknown",
        "id": None,
        "parent_session_id": _env(env, "parent"),
        "task": task,
        "pr": None,
    }
    if kind in SPAWN_KINDS:
        # Explicit child launches outrank inherited event/PM/selection state.
        # Fanout carries its selected task; workers must not borrow that task.
        cause.update(kind=kind, id=_env(env, "id"), task=None if kind == "worker" else task)
    elif env.get("EVENT_ID"):
        cause.update(
            kind="event",
            id=env["EVENT_ID"],
            pr=_pr_ref(env.get("EVENT_URL", "")),
            event_type=env.get("EVENT_TRIGGER_TYPE") or None,
        )
    elif env.get("PM_DISPATCH_ID"):
        items = _pm_items(env.get("PM_WORK_FILE", ""))
        types = {kind_ for item in items for kind_ in _types(item)}
        # Several items can share a slot. Never arbitrarily credit its first PR.
        prs = {_item_pr(item) for item in items}
        prs.discard(None)
        cause.update(
            kind="agent-msg" if types == {"agent_msg_reply"} else "pm-slot",
            id=env["PM_DISPATCH_ID"],
            pr=next(iter(prs)) if len(prs) == 1 else None,
        )
    elif task:
        cause.update(kind="cascade-task", id=env.get("CASCADE_COORDINATION_KEY") or task)
    elif env.get("INVOCATION_ID"):
        # systemd also starts manually requested services; its invocation ID
        # establishes a service run, not proof that a timer fired.
        cause.update(kind="systemd", id=env["INVOCATION_ID"])
    return cause


def last_release(workspace: Path, task: str) -> dict[str, Any] | None:
    """Return the task's last recorded wait release — a precursor, not a parent."""
    path = workspace / "state/wait-resolutions.jsonl"
    latest = None
    try:
        with path.open() as stream:
            for line in stream:
                row = _object(line)
                if row.get("task_id") == task and row.get("ts"):
                    latest = {
                        "id": row.get("id") or f"wait:{task}:{row['ts']}",
                        "ts": row["ts"],
                        "kind": row.get("evidence_type"),
                    }
    except (OSError, UnicodeError):
        pass
    return latest


def session_trigger(cause: Mapping[str, Any]) -> str:
    """Classify a cause into the spawn/dispatch/timer vocabulary.

    A ``systemd`` cause stays ``unknown``: an invocation ID proves a service ran,
    not that a timer fired it (a manual ``systemctl start`` looks identical).
    """
    kind = cause.get("kind")
    if kind in SPAWN_KINDS:
        return "spawn"
    if kind in DISPATCH_KINDS:
        return "dispatch"
    return "timer" if kind == "timer" else "unknown"


def append_start(workspace: Path, env: Mapping[str, str], cause: Mapping[str, Any]) -> None:
    """Append one start row under an exclusive lock.

    The ledger is append-only and directory-created so a first run in a fresh
    workspace succeeds without extra setup.
    """
    session_id = _env(env, "session")
    receipt = ALIASES["receipt"]
    if any(name in env for name in receipt):
        # An empty selection must not borrow another backend's native ID.
        session_id = next((env[name] for name in receipt if name in env), "") or None
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "dispatch_cause": dict(cause),
    }
    path = workspace / "state/sessions/dispatch-starts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def resolve_cause(workspace: Path, env: Mapping[str, str], refresh: bool = False) -> dict[str, Any]:
    """Return the inherited cause, or infer a fresh one when ``refresh``."""
    cause = _object(_env(env, "cause") or "")
    if refresh or not isinstance(cause.get("kind"), str):
        cause = infer_cause(env)
        task = cause.get("task")
        if isinstance(task, str):
            release = last_release(workspace, task)
            if release:
                cause["release"] = release
    return cause


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--record-start", action="store_true")
    parser.add_argument("--trigger", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Start a new launch rather than preserving the parent handoff",
    )
    args = parser.parse_args(argv)
    env = os.environ
    cause = resolve_cause(args.workspace, env, refresh=args.refresh)
    if args.record_start:
        append_start(args.workspace, env, cause)
    print(session_trigger(cause) if args.trigger else json.dumps(cause, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
