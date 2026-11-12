"""Decision rules for ``gptme-runloops select --lane`` (pure functions).

Steps reasoning effort down under pool pressure, chooses the lane arm or a
fallback, and formats the decision. Inputs are already-read facts (pool
reading, exclusions, unavailable arms); no I/O happens here.

Rules, first that applies: ``excluded_fallback``, ``lane``, ``lane_pressure``,
``fallback``, ``no_capacity``. ``fail_open`` is produced by the caller
(:mod:`gptme_runloops.select_lane`) on any internal error.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .select_policy import Arm, LaneRow, PoolPolicy
from .select_usage import PoolReading


def compute_effort(
    base: str | None,
    ladder: tuple[str, ...],
    pool: PoolPolicy | None,
    reading: PoolReading,
) -> tuple[str | None, str]:
    """Effort for the lane arm and a one-line reason.

    The highest effort step whose ``at`` is reached applies (steps do not
    stack); the pace guard forces at least one step down when the pool runs
    more than ``pace_guard`` ahead of even pace (max with the step, not sum).
    Unknown usage runs ``usage_unknown`` (default: base). Never steps up.
    """
    if base is None:
        return None, "lane has no effort setting"
    if pool is None:
        return base, f"no pool: base effort {base}"
    idx = ladder.index(base)
    if reading.used is None:
        note = reading.note or "usage unknown"
        if pool.usage_unknown == "base":
            return base, f"{note}: base effort {base}"
        fixed = ladder[max(idx, ladder.index(pool.usage_unknown))]
        return fixed, f"{note}: usage_unknown -> {fixed}"
    target = idx
    why: list[str] = []
    reached = [s for s in pool.effort_steps if reading.used >= s.at]
    if reached:
        step = reached[-1]
        stepped = idx + step.down if step.down else ladder.index(step.to or base)
        target = max(idx, stepped)
        why.append(f"weekly {reading.used:.2f} >= {step.at:.2f}")
    gap = reading.pace_gap
    if (
        pool.pace_guard is not None
        and gap is not None
        and gap > pool.pace_guard
        and target < idx + 1
    ):
        target = idx + 1
        why.append(f"pace gap {gap:+.2f} > {pool.pace_guard:.2f}")
    effort = ladder[min(target, len(ladder) - 1)]
    if effort == base:
        detail = f"weekly {reading.used:.2f}" + (
            f", pace gap {gap:+.2f}" if gap is not None else ""
        )
        return base, f"{detail}: base effort {base}"
    return effort, f"{' and '.join(why)}: {base} -> {effort}"


@dataclass
class Choice:
    rule: str
    arm: Arm | None
    effort: str | None
    scope: str | None
    reason: str
    skipped: list[dict[str, str]] = field(default_factory=list)


def choose_arm(
    row: LaneRow,
    pool: PoolPolicy | None,
    reading: PoolReading,
    excluded: Mapping[str, str | None],
    unavailable: Mapping[str, tuple[str, str]],
) -> Choice:
    """Pick the lane arm or a fallback.

    ``excluded`` maps ``backend:model`` to the failure reason passed with
    ``--exclude``; ``unavailable`` maps an arm to ``(trigger, why)`` where
    trigger is ``blocked`` or ``missing``.
    """
    lane_arm = str(row.arm)
    threshold = pool.capacity_threshold if pool else None
    on_success = "fallback"
    if lane_arm in excluded:
        trigger = excluded[lane_arm] or "unspecified"
        why, on_success = f"excluded ({trigger})", "excluded_fallback"
    elif lane_arm in unavailable:
        trigger, why = unavailable[lane_arm]
    elif (
        threshold is not None and reading.used is not None and reading.used >= threshold
    ):
        trigger = "no_capacity"
        why = f"pool weekly {reading.used:.2f} >= no_capacity_at {threshold:.2f}"
    else:
        effort, reason = compute_effort(row.effort, row.ladder, pool, reading)
        rule = "lane" if effort == row.effort else "lane_pressure"
        return Choice(rule, row.arm, effort, row.scope, reason)
    skipped = [{"arm": lane_arm, "why": why}]
    lead = why if trigger == "no_capacity" else f"lane arm {why}"
    for fb in row.fallback:
        arm = str(fb.arm)
        if fb.on is not None and trigger not in fb.on:
            skipped.append(
                {"arm": arm, "why": f"on {sorted(fb.on)} does not match {trigger}"}
            )
        elif arm in excluded:
            skipped.append(
                {"arm": arm, "why": f"excluded ({excluded[arm] or 'unspecified'})"}
            )
        elif arm in unavailable:
            skipped.append({"arm": arm, "why": unavailable[arm][1]})
        else:
            return Choice(
                on_success,
                fb.arm,
                fb.effort,
                fb.scope,
                f"{lead}: fallback {arm}",
                skipped,
            )
    return Choice(
        "no_capacity", None, None, None, f"{lead}; no fallback applies", skipped
    )


def pair_exclusions(arms: list[str], reasons: list[str]) -> dict[str, str | None]:
    """Pair ``--exclude`` with ``--reason`` by position; a single reason applies to all."""
    out: dict[str, str | None] = {}
    for i, arm in enumerate(arms):
        if i < len(reasons):
            out[arm] = reasons[i]
        else:
            out[arm] = reasons[-1] if len(reasons) == 1 else None
    return out


_SHELL_FIELDS = (
    ("ROUTE_BACKEND", "backend"),
    ("ROUTE_MODEL", "model"),
    ("ROUTE_EFFORT", "effort"),
    ("ROUTE_SCOPE", "scope"),
    ("ROUTE_RULE", "rule"),
    ("ROUTE_REASON", "reason"),
    ("ROUTE_DECISION_ID", "decision_id"),
)


def format_shell(decision: Mapping[str, Any]) -> str:
    """``KEY=value`` lines, each value shell-quoted; null becomes empty."""
    lines = []
    for var, key in _SHELL_FIELDS:
        value = decision.get(key)
        lines.append(f"{var}={shlex.quote('' if value is None else str(value))}")
    return "\n".join(lines)


def format_explain(decision: Mapping[str, Any]) -> str:
    """Human-readable trace of a decision."""
    pool = decision.get("pool")
    arm = (
        f"{decision.get('backend')}:{decision.get('model')}"
        if decision.get("backend")
        else "(none)"
    )
    lines = [
        f"lane: {decision.get('lane')}  class: {decision.get('class') or '-'}  work item: {decision.get('work_item') or '-'}",
        f"policy: {decision.get('policy_version') or '-'}"
        + ("  (EXPIRED row)" if decision.get("policy_expired") else ""),
    ]
    if pool:
        used = "unknown" if pool["used"] is None else f"{pool['used']:.2f}"
        gap = "unknown" if pool["pace_gap"] is None else f"{pool['pace_gap']:+.3f}"
        age = "-" if pool["age_s"] is None else f"{pool['age_s']}s"
        lines.append(
            f"pool: {pool['name']}  used {used}  pace gap {gap}  age {age}  source {pool['source']}"
        )
    for s in decision.get("skipped") or []:
        lines.append(f"skipped: {s['arm']}: {s['why']}")
    lines.append(f"rule: {decision.get('rule')}  ({decision.get('reason')})")
    lines.append(
        f"run: {arm}  effort {decision.get('effort') or '-'}  scope {decision.get('scope') or '-'}"
    )
    if decision.get("shadow"):
        lines.append("shadow: decision logged only; exit code forced to 0")
    return "\n".join(lines)
