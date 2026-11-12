"""``gptme-runloops select --lane``: one routing decision per call, never fatal.

Reads the lane policy from ``harness-quota.toml`` (:mod:`.select_policy`), the
pool's usage (:mod:`.select_usage`), the block registry and the backend
binaries, applies the rules (:mod:`.select_rules`), appends the decision to a
JSONL log and returns it with an exit code.

Fail open: any internal error (unreadable or invalid config, unknown schema
version, unknown lane, unexpected exception) yields rule ``fail_open`` for
``--default-arm`` when given (exit 0), else exit 2 with the error. A broken
usage provider reads as "usage unknown" (base effort, never ``no_capacity``).
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import select as _select
from .select_policy import NO_CAPACITY_EXIT, TRIGGERS, PolicyError, parse_routing
from .select_rules import choose_arm
from .select_usage import PoolReading, read_pool_usage

FAIL_OPEN_NO_DEFAULT_EXIT = 2
#: ``no_arm``: the lane arm is excluded, blocked or missing and no fallback
#: applies. Not a quota skip: callers run their default and report an outage.
NO_ARM_EXIT = 69  # EX_UNAVAILABLE


@dataclass
class LaneOutcome:
    """Result of :func:`select_lane`: the decision, its exit code, any error."""

    decision: dict[str, Any]
    exit_code: int
    error: str | None = None


def _config_path(config_path: Path | None) -> Path:
    return config_path or _select._DEFAULT_CONFIG_PATHS[0]


def _load_raw(config_path: Path | None) -> dict[str, Any]:
    with _config_path(config_path).open("rb") as f:
        return _select.tomllib.load(f)


def _lane_block(
    state_dir: Path, backend: str, model: str, pool: str | None
) -> str | None:
    """Why the arm is blocked (backend, arm or pool block file), or None.

    Unlike the ``[[candidates]]`` path, this fails *open* when the block
    registry is not importable: its own breakage must not stop the agent.
    """
    try:
        from gptme_block_registry.contract import (
            ARM_BLOCK_KINDS,
            arm_block_path,
            backend_block_path,
            pool_block_path,
        )
        from gptme_block_registry.writers import read_block_until
    except ImportError:
        return None
    now = datetime.now(tz=timezone.utc)
    paths = [backend_block_path(state_dir, backend)]
    paths += [arm_block_path(state_dir, backend, model, k) for k in ARM_BLOCK_KINDS]
    if pool:
        paths.append(pool_block_path(state_dir, pool))
    for path in paths:
        until = read_block_until(path)
        if until is not None and until > now:
            return f"blocked until {until.isoformat()} ({path.name})"
    return None


def _append_log(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line; failure is reported on stderr, never raised."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        print(f"[select] decision log not written ({path}): {exc}", file=sys.stderr)


def _new_decision(
    lane: str, cls: str | None, work_item: str | None, shadow: bool
) -> dict[str, Any]:
    return {
        "backend": None, "model": None, "effort": None, "scope": None,
        "rule": "fail_open", "reason": "", "lane": lane, "class": cls,
        "work_item": work_item, "decision_id": str(uuid.uuid4()),
        "policy_version": None, "policy_expired": False, "shadow": shadow,
        "pool": None, "skipped": [], "trigger": None,
    }  # fmt: skip


def _decide(
    decision: dict[str, Any],
    config_path: Path | None,
    state_dir: Path,
    excluded: dict[str, str | None],
    today: date,
) -> None:
    """Fill ``decision`` from the lane policy. Raises on internal errors."""
    policy = parse_routing(_load_raw(config_path), today)
    decision["policy_version"] = policy.version
    row = policy.row(decision["lane"], decision["class"])
    if row.cls is None:
        decision["class"] = None  # no such class row: the lane row decided
    pool = policy.pools.get(row.pool) if row.pool else None
    reading = (
        read_pool_usage(pool.provider, pool.options)
        if pool
        else PoolReading(source="none")
    )
    unavailable: dict[str, tuple[str, str]] = {}
    for arm in [row.arm, *(fb.arm for fb in row.fallback)]:
        if not _select._binary_available(arm.backend):
            binary = _select._backend_binary(arm.backend)
            unavailable[str(arm)] = ("missing", f"binary {binary!r} not found")
            continue
        arm_pool = row.pool if arm == row.arm else None
        blocked = _lane_block(state_dir, arm.backend, arm.model, arm_pool)
        if blocked:
            unavailable[str(arm)] = ("blocked", blocked)
    choice = choose_arm(row, pool, reading, excluded, unavailable)
    decision.update(
        backend=choice.arm.backend if choice.arm else None,
        model=choice.arm.model if choice.arm else None,
        effort=choice.effort, scope=choice.scope, rule=choice.rule,
        reason=choice.reason, skipped=choice.skipped, trigger=choice.trigger,
        policy_expired=row.expired(today),
        pool=reading.to_dict(pool.name) if pool else None,
    )  # fmt: skip


def _known_reasons(
    excluded: dict[str, str | None],
) -> tuple[dict[str, str | None], list[str]]:
    """Map ``--reason`` values outside the trigger vocabulary to ``unspecified``."""
    out: dict[str, str | None] = {}
    notes: list[str] = []
    for arm, reason in excluded.items():
        if reason is not None and reason not in TRIGGERS:
            notes.append(f"unknown --reason {reason!r} treated as unspecified")
            reason = None
        out[arm] = reason
    return out, notes


def _fail_open(
    decision: dict[str, Any], error: str, default_arm: str | None, effort: str | None
) -> str:
    """Turn ``decision`` into a ``fail_open`` one; returns the error text."""
    # Lenient on purpose: this is what the caller would run anyway.
    backend, _, model = (default_arm or "").partition(":")
    usable = bool(backend and model)
    if default_arm and not usable:
        error += f"; --default-arm {default_arm!r} is not 'backend:model'"
    decision.update(
        backend=backend if usable else None,
        model=model if usable else None,
        effort=effort if usable else None,
        scope=None, rule="fail_open", reason=error, pool=None, skipped=[],
        trigger=None,
    )  # fmt: skip
    return error


def select_lane(
    lane: str,
    *,
    cls: str | None = None,
    work_item: str | None = None,
    excluded: dict[str, str | None] | None = None,
    default_arm: str | None = None,
    default_effort: str | None = None,
    shadow: bool = False,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    log_path: Path | None = None,
    today: date | None = None,
) -> LaneOutcome:
    """Decide what ``lane`` runs now, log the decision, and never raise.

    Exit codes: 0 with a decision; 76 (:data:`NO_CAPACITY_EXIT`) for
    ``no_capacity``; 69 (:data:`NO_ARM_EXIT`) for ``no_arm``; 2 after an
    internal error without ``default_arm``.
    ``shadow`` always exits 0.
    """
    state_dir = state_dir or _select._DEFAULT_STATE_DIR
    decision = _new_decision(lane, cls, work_item, shadow)
    if default_arm:
        decision["default"] = {"arm": default_arm, "effort": default_effort}
    error: str | None = None
    excluded, notes = _known_reasons(excluded or {})
    try:
        _decide(decision, config_path, state_dir, excluded, today or date.today())
    except Exception as exc:  # fail open on anything, by contract
        text = (
            str(exc) if isinstance(exc, PolicyError) else f"{type(exc).__name__}: {exc}"
        )
        error = _fail_open(decision, text, default_arm, default_effort)
    if notes:
        decision["reason"] = "; ".join([decision["reason"], *notes])
    decision["ts"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    _append_log(log_path or state_dir / "routing" / "decisions.jsonl", decision)
    if shadow:
        code = 0
    elif decision["rule"] == "fail_open" and decision["backend"] is None:
        code = FAIL_OPEN_NO_DEFAULT_EXIT
    elif decision["rule"] == "no_capacity":
        code = NO_CAPACITY_EXIT
    elif decision["rule"] == "no_arm":
        code = NO_ARM_EXIT
    else:
        code = 0
    return LaneOutcome(decision, code, error)


def validate_policy(
    config_path: Path | None = None, today: date | None = None
) -> tuple[list[str], int]:
    """Report problems in the routing tables; exit 1 only for errors.

    Errors are the problems that would make ``select --lane`` fail open;
    warnings (expired rows, unknown keys or triggers) never fail.
    """
    try:
        raw = _load_raw(config_path)
    except Exception as exc:
        return [f"error: {_config_path(config_path)}: {type(exc).__name__}: {exc}"], 1
    policy = parse_routing(raw, today or date.today())
    lines = [str(i) for i in policy.issues]
    errors = sum(1 for i in policy.issues if i.level == "error")
    warnings = len(policy.issues) - errors
    lines.append(
        f"{'FAIL' if errors else 'ok'}: {len(policy.lanes)} lane(s), "
        f"{len(policy.pools)} pool(s), policy {policy.version}, "
        f"{errors} error(s), {warnings} warning(s)"
    )
    return lines, 1 if errors else 0
