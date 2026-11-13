"""Lane routing config for ``gptme-runloops select --lane`` (pure functions).

Parses and validates the ``[routing]``, ``[pools.*]`` and ``[lanes.*]`` tables
of ``harness-quota.toml`` into a :class:`RoutingPolicy`. Problems are collected
as :class:`Issue` rows: errors make the affected lane (or, for global errors,
every lane) fail open; warnings are reported by ``--validate`` only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .select_usage import PROVIDERS, USAGE_OPTION_KEYS

SCHEMA_VERSION = 1
NO_CAPACITY_EXIT = 76
DEFAULT_PROVIDER = "claude-usage"
#: Default ladder, highest first; a lane's default ladder starts at its base.
DEFAULT_LADDER = ("max", "xhigh", "high", "medium", "low")
#: Values a fallback's ``on`` list may name.
TRIGGERS = frozenset(
    {"rate_limited", "auth", "crash", "blocked", "missing", "no_capacity"}
)
_BARE_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})
_POOL_KEYS = (
    frozenset(
        {
            "provider",
            "owner_reserve",
            "no_capacity_at",
            "effort_steps",
            "pace_guard",
            "usage_unknown",
        }
    )
    | USAGE_OPTION_KEYS
)
_ROW_KEYS = frozenset(
    {"arm", "effort", "effort_ladder", "pool", "expires", "fallback", "scope"}
)
_FALLBACK_KEYS = frozenset({"arm", "scope", "on", "effort"})


class PolicyError(ValueError):
    """The policy cannot produce a decision for this lane (caller fails open)."""


def is_bare_alias(model: str) -> bool:
    """True for floating CLI aliases (``opus``, ``claude-sonnet``, ...)."""
    try:
        from gptme_usage.harness_models import is_floating_cc_alias
    except Exception:  # optional package missing or broken
        m = model.lower().strip()
        return (m[len("claude-") :] if m.startswith("claude-") else m) in _BARE_ALIASES
    return is_floating_cc_alias(model)


@dataclass(frozen=True)
class Arm:
    backend: str
    model: str

    def __str__(self) -> str:
        return f"{self.backend}:{self.model}"


def parse_arm(text: object) -> Arm:
    """Parse ``backend:model`` (model may itself contain ``:``); reject bare aliases."""
    if not isinstance(text, str) or ":" not in text:
        raise ValueError(f"arm must be 'backend:model', got {text!r}")
    backend, model = (s.strip() for s in text.split(":", 1))
    if not backend or not model:
        raise ValueError(f"arm must be 'backend:model', got {text!r}")
    if is_bare_alias(model):
        raise ValueError(
            f"arm {text!r} uses bare alias {model!r}; use an explicit model id"
        )
    return Arm(backend, model)


@dataclass(frozen=True)
class EffortStep:
    at: float
    down: int | None = None
    to: str | None = None


@dataclass(frozen=True)
class PoolPolicy:
    name: str
    provider: str = DEFAULT_PROVIDER
    options: Mapping[str, Any] = field(default_factory=dict)
    owner_reserve: float | None = None
    no_capacity_at: float | None = None
    effort_steps: tuple[EffortStep, ...] = ()
    pace_guard: float | None = None
    usage_unknown: str = "base"

    @property
    def capacity_threshold(self) -> float | None:
        """``no_capacity_at``, else ``1 - owner_reserve``, else no ceiling."""
        if self.no_capacity_at is not None:
            return self.no_capacity_at
        if self.owner_reserve is not None:
            return round(1.0 - self.owner_reserve, 6)
        return None


@dataclass(frozen=True)
class FallbackEntry:
    arm: Arm
    scope: str | None = None
    on: frozenset[str] | None = None  # None: any trigger
    effort: str | None = None


@dataclass(frozen=True)
class LaneRow:
    lane: str
    cls: str | None
    arm: Arm
    effort: str | None = None
    ladder: tuple[str, ...] = ()
    pool: str | None = None
    expires: date | None = None
    fallback: tuple[FallbackEntry, ...] = ()
    scope: str | None = None

    def expired(self, today: date) -> bool:
        return self.expires is not None and today > self.expires


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    where: str
    message: str
    lane: str | None = None  # errors scoped to one lane only stop that lane

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass
class RoutingPolicy:
    pools: dict[str, PoolPolicy] = field(default_factory=dict)
    rows: dict[tuple[str, str | None], LaneRow] = field(default_factory=dict)
    lanes: set[str] = field(default_factory=set)
    version: str = ""
    issues: list[Issue] = field(default_factory=list)

    def errors_for(self, lane: str | None) -> list[Issue]:
        return [
            i
            for i in self.issues
            if i.level == "error" and (i.lane is None or i.lane == lane)
        ]

    def row(self, lane: str, cls: str | None) -> LaneRow:
        """Class row when present, else the lane row. Raises :class:`PolicyError`."""
        errors = self.errors_for(lane)
        if errors:
            raise PolicyError("; ".join(str(e) for e in errors))
        if lane not in self.lanes:
            raise PolicyError(f"unknown lane {lane!r}")
        if (lane, cls) in self.rows:
            return self.rows[(lane, cls)]
        return self.rows[(lane, None)]


def policy_version(raw: Mapping[str, Any]) -> str:
    """Stable short hash of the routing tables (``routing``, ``pools``, ``lanes``)."""
    tables = {k: raw.get(k) for k in ("routing", "pools", "lanes")}
    blob = json.dumps(tables, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


@dataclass
class _Where:
    """Issue sink for one table; ``lane`` scopes errors to that lane only."""

    issues: list[Issue]
    lane: str | None
    at: str

    def err(self, msg: str, sub: str = "") -> None:
        self.issues.append(Issue("error", self.at + sub, msg, self.lane))

    def warn(self, msg: str, sub: str = "") -> None:
        self.issues.append(Issue("warning", self.at + sub, msg, self.lane))


def _fraction(value: object, w: _Where, sub: str) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        value = None
    if value is None or not 0 <= value <= 1:
        w.err(f"must be a number in [0, 1], got {value!r}", sub)
        return None
    return float(value)


def _parse_steps(raw: object, w: _Where) -> tuple[EffortStep, ...]:
    steps: list[EffortStep] = []
    for i, step in enumerate(raw if isinstance(raw, list) else [None]):
        sub = f".effort_steps[{i}]"
        if not isinstance(step, dict) or ("down" in step) == ("to" in step):
            w.err("must be {at = 0..1, down = n} or {at = 0..1, to = effort}", sub)
            continue
        at = _fraction(step.get("at"), w, sub + ".at")
        down, to = step.get("down"), step.get("to")
        if down is not None and (
            isinstance(down, bool) or not isinstance(down, int) or down < 1
        ):
            w.err(f"must be a positive integer, got {down!r}", sub + ".down")
        elif to is not None and not (isinstance(to, str) and to):
            w.err(f"must be an effort name, got {to!r}", sub + ".to")
        elif at is not None:
            steps.append(EffortStep(at, down, to))
    return tuple(sorted(steps, key=lambda s: s.at))


def _parse_pool(name: str, raw: object, issues: list[Issue]) -> PoolPolicy | None:
    w = _Where(issues, None, f"pools.{name}")
    if not isinstance(raw, dict):
        w.err("must be a table")
        return None
    for key in sorted(set(raw) - _POOL_KEYS):
        w.warn(f"unknown key {key!r}")
    provider = raw.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        w.err(f"unknown provider {provider!r} (known: {', '.join(sorted(PROVIDERS))})")
    frac = {
        key: _fraction(raw[key], w, f".{key}")
        for key in ("owner_reserve", "no_capacity_at", "pace_guard")
        if key in raw
    }
    usage_unknown = raw.get("usage_unknown", "base")
    if not isinstance(usage_unknown, str) or not usage_unknown:
        w.err(f"must be 'base' or an effort, got {usage_unknown!r}", ".usage_unknown")
        usage_unknown = "base"
    nca, reserve = frac.get("no_capacity_at"), frac.get("owner_reserve")
    if nca is not None and reserve is not None and nca > 1 - reserve + 1e-9:
        w.warn(f"no_capacity_at {nca} leaves less than owner_reserve {reserve}")
    return PoolPolicy(
        name=name,
        provider=str(provider),
        options={k: raw[k] for k in USAGE_OPTION_KEYS if k in raw},
        owner_reserve=reserve,
        no_capacity_at=nca,
        effort_steps=_parse_steps(raw.get("effort_steps", []), w),
        pace_guard=frac.get("pace_guard"),
        usage_unknown=usage_unknown,
    )


def _parse_expires(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"expires must be a date, got {value!r}")


def _parse_ladder(raw: dict, effort: str | None, w: _Where) -> tuple[str, ...]:
    if "effort_ladder" in raw:
        lad = raw["effort_ladder"]
        if not isinstance(lad, list) or not all(isinstance(e, str) and e for e in lad):
            w.err(f"must be a list of effort names, got {lad!r}", ".effort_ladder")
            return ()
        ladder = tuple(lad)
    elif effort in DEFAULT_LADDER:
        ladder = DEFAULT_LADDER[DEFAULT_LADDER.index(effort) :]
    else:
        ladder = (effort,) if effort else ()
    if effort is not None and effort not in ladder:
        w.err(
            f"{list(ladder)} does not contain base effort {effort!r}", ".effort_ladder"
        )
    return ladder


def _check_pool(
    name: object,
    pools: Mapping[str, PoolPolicy],
    effort: str | None,
    ladder: tuple[str, ...],
    w: _Where,
) -> None:
    if name is None:
        return
    pool = pools.get(name) if isinstance(name, str) else None
    if pool is None:
        w.err(f"unknown pool {name!r}", ".pool")
        return
    if not ladder:
        return
    for step in pool.effort_steps:
        if step.to is not None and step.to not in ladder:
            w.err(f"pool {name!r} steps to {step.to!r}, not in ladder {list(ladder)}")
    if pool.usage_unknown != "base" and pool.usage_unknown not in ladder:
        w.err(f"pool {name!r} usage_unknown {pool.usage_unknown!r} is not in ladder")
    if effort in ladder:
        _warn_non_monotonic(pool, ladder.index(effort), ladder, w)


def _warn_non_monotonic(
    pool: PoolPolicy, base: int, ladder: tuple[str, ...], w: _Where
) -> None:
    """Warn when a higher ``at`` resolves to a higher effort than a lower one.

    The highest reached step wins, so such a step raises effort again as usage
    grows. Positions are resolved as in ``select_rules.compute_effort``.
    """
    deepest, deepest_at = base, None
    for step in pool.effort_steps:
        if step.to is not None and step.to not in ladder:
            return  # already an error
        pos = base + step.down if step.down else ladder.index(step.to or "")
        pos = min(max(base, pos), len(ladder) - 1)
        if pos < deepest:
            w.warn(
                f"pool {pool.name!r} effort_steps step back up at {step.at}: "
                f"{ladder[pos]} after {ladder[deepest]} at {deepest_at}"
            )
            return
        deepest, deepest_at = pos, step.at


def _parse_fallbacks(raw: object, w: _Where) -> tuple[FallbackEntry, ...]:
    out: list[FallbackEntry] = []
    for i, entry in enumerate(raw if isinstance(raw, list) else [None]):
        sub = f".fallback[{i}]"
        if not isinstance(entry, dict):
            w.err("must be a table {arm = ..., scope = ..., on = [...]}", sub)
            continue
        for key in sorted(set(entry) - _FALLBACK_KEYS):
            w.warn(f"unknown key {key!r}", sub)
        on = entry.get("on")
        if on is not None and (
            not isinstance(on, list) or not all(isinstance(t, str) for t in on)
        ):
            w.err(f"on must be a list of strings, got {on!r}", sub + ".on")
            continue
        for t in sorted(set(on or ()) - TRIGGERS):
            w.warn(f"unknown trigger {t!r} (known: {', '.join(sorted(TRIGGERS))})", sub)
        try:
            arm = parse_arm(entry.get("arm"))
        except ValueError as exc:
            w.err(str(exc), sub + ".arm")
            continue
        effort = entry.get("effort")
        if effort is not None and not (isinstance(effort, str) and effort):
            w.err(f"effort must be a non-empty string, got {effort!r}", sub)
            continue
        scope = entry.get("scope")
        on_set = frozenset(on) if on is not None else None
        out.append(FallbackEntry(arm, scope, on_set, effort))
    return tuple(out)


def _parse_row(
    lane: str,
    cls: str | None,
    raw: dict,
    pools: Mapping[str, PoolPolicy],
    issues: list[Issue],
    today: date,
) -> LaneRow | None:
    w = _Where(issues, lane, f"lanes.{lane}" + (f".{cls}" if cls else ""))
    n_errors = len(issues)
    for key in sorted(set(raw) - _ROW_KEYS):
        w.warn(f"unknown key {key!r}")
    try:
        arm: Arm | None = parse_arm(raw.get("arm"))
    except ValueError as exc:
        w.err(str(exc), ".arm")
        arm = None
    effort = raw.get("effort")
    if effort is not None and not (isinstance(effort, str) and effort):
        w.err(f"must be a non-empty string, got {effort!r}", ".effort")
        effort = None
    ladder = _parse_ladder(raw, effort, w)
    _check_pool(raw.get("pool"), pools, effort, ladder, w)
    expires = None
    if "expires" in raw:
        try:
            expires = _parse_expires(raw["expires"])
        except ValueError as exc:
            w.err(str(exc), ".expires")
        else:
            if today > expires:
                w.warn(f"expired {expires} (still used; decisions say policy_expired)")
    fallback = _parse_fallbacks(raw.get("fallback", []), w)
    _warn_unknown_effort(arm, effort, w)
    errors = [i for i in issues[n_errors:] if i.level == "error"]
    if arm is None or errors:
        return None
    pool = raw.get("pool")
    return LaneRow(
        lane, cls, arm, effort, ladder, pool, expires, fallback, raw.get("scope")
    )


def _warn_unknown_effort(arm: Arm | None, effort: str | None, w: _Where) -> None:
    """Warn (never fail) when an effort is not documented for the backend."""
    if arm is None or effort is None:
        return
    try:
        from gptme_sessions.record import KNOWN_REASONING_EFFORTS
    except Exception:  # optional package missing or broken
        return
    known = KNOWN_REASONING_EFFORTS.get(arm.backend)
    if known is not None and effort.lower() not in known:
        w.warn(f"{effort!r} is not a documented effort for {arm.backend}", ".effort")


def parse_routing(raw: Mapping[str, Any], today: date) -> RoutingPolicy:
    """Parse the routing tables; problems are collected in ``policy.issues``."""
    policy = RoutingPolicy(version=policy_version(raw))
    issues = policy.issues
    routing = raw.get("routing")
    version = routing.get("schema_version") if isinstance(routing, dict) else None
    if version != SCHEMA_VERSION:
        issues.append(
            Issue(
                "error",
                "routing.schema_version",
                f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})",
            )
        )
    pools_raw = raw.get("pools", {})
    for name, pool_raw in pools_raw.items() if isinstance(pools_raw, dict) else []:
        pool = _parse_pool(name, pool_raw, issues)
        if pool is not None:
            policy.pools[name] = pool
    lanes_raw = raw.get("lanes", {})
    if not isinstance(lanes_raw, dict):
        issues.append(Issue("error", "lanes", "must be a table"))
        return policy
    for lane, lane_raw in lanes_raw.items():
        if not isinstance(lane_raw, dict):
            issues.append(Issue("error", f"lanes.{lane}", "must be a table", lane))
            continue
        policy.lanes.add(lane)
        base = {k: v for k, v in lane_raw.items() if not isinstance(v, dict)}
        row = _parse_row(lane, None, base, policy.pools, issues, today)
        if row is not None:
            policy.rows[(lane, None)] = row
        for cls, cls_raw in lane_raw.items():
            if isinstance(cls_raw, dict):
                row = _parse_row(
                    lane, cls, {**base, **cls_raw}, policy.pools, issues, today
                )
                if row is not None:
                    policy.rows[(lane, cls)] = row
    return policy
