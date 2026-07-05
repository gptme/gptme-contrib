"""Configuration for the subscription manager CLI.

Centralises all paths, thresholds, and operator preferences so the
manager logic can stay free of hard-coded workspace assumptions. Values
load from (in priority order):

    explicit constructor argument > env var > XDG default

Env vars use the ``GPTME_SUBSCRIPTION_`` prefix. See ``Config.from_env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _xdg_state_home() -> Path:
    """Return the XDG state directory (default ``~/.local/state``)."""
    env = os.environ.get("XDG_STATE_HOME")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state"


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key)
    return Path(raw) if raw else default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(key)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    """All manager dependencies, paths, and thresholds."""

    # --- Slot identity ---
    subscriptions: list[str] = field(
        default_factory=lambda: _env_list("GPTME_SUBSCRIPTION_SLOTS", ["primary"])
    )
    fallback_order: list[str] = field(
        default_factory=lambda: _env_list("GPTME_SUBSCRIPTION_FALLBACK_ORDER", [])
    )
    primary: str = ""  # First entry of ``subscriptions`` unless overridden.

    # --- Credential storage ---
    creds_dir: Path = field(
        default_factory=lambda: _env_path(
            "GPTME_SUBSCRIPTION_CREDS_DIR", Path.home() / ".claude"
        )
    )
    creds_live_name: str = ".credentials.json"
    slot_template: str = ".credentials.json.{sub}"
    fingerprint_template: str = ".credentials.json.{sub}.fingerprint.json"

    # --- State storage (logs, rebalance, observations) ---
    state_dir: Path = field(
        default_factory=lambda: _env_path(
            "GPTME_SUBSCRIPTION_STATE_DIR",
            _xdg_state_home() / "gptme-subscription",
        )
    )

    # --- Usage check ---
    # Path to a script that prints usage JSON when invoked with --json. The
    # output shape is documented in ``manager.UsageProbe.run``.
    usage_script: Path | None = field(
        default_factory=lambda: (
            Path(p)
            if (p := os.environ.get("GPTME_SUBSCRIPTION_USAGE_SCRIPT"))
            else None
        )
    )

    # --- Autonomous session guard ---
    # Glob for lock files held by long-running agent sessions. When at least
    # one referenced PID is alive, automated switches are deferred to prevent
    # mid-session credential flips. Defaults to an empty glob (no guard).
    lock_glob: str = field(
        default_factory=lambda: os.environ.get("GPTME_SUBSCRIPTION_LOCK_GLOB", "")
    )

    # --- Thresholds: when to consider a slot exhausted ---
    weekly_exhausted: float = field(
        default_factory=lambda: _env_float("GPTME_SUBSCRIPTION_WEEKLY_EXHAUSTED", 0.85)
    )
    five_hour_exhausted: float = field(
        default_factory=lambda: _env_float(
            "GPTME_SUBSCRIPTION_FIVE_HOUR_EXHAUSTED", 0.90
        )
    )
    sonnet_weekly_exhausted: float = field(
        default_factory=lambda: _env_float(
            "GPTME_SUBSCRIPTION_SONNET_WEEKLY_EXHAUSTED", 0.95
        )
    )

    # --- Probe / rebalance ---
    probe_primary_cooldown: int = field(
        default_factory=lambda: _env_int(
            "GPTME_SUBSCRIPTION_PROBE_COOLDOWN",
            1800,  # 30 min
        )
    )
    rebalance_ahead_threshold: float = 0.10
    rebalance_target_utilization: float = 0.90
    rebalance_min_hold: int = 6 * 3600
    rebalance_max_hold: int = 48 * 3600

    # --- Forward routing ---
    forward_routing_period_threshold: float = 0.25
    forward_routing_idle_frac: float = 0.30
    forward_routing_hold_seconds: int = 8 * 3600
    soon_to_expire_threshold: int = 12 * 3600
    expiring_capacity_credit: float = 0.25
    unknown_fallback_pressure: float = 0.50
    capacity_rebalance_min_pressure: float = 0.70
    capacity_rebalance_margin: float = 0.25

    # --- Rate-limit indicator ---
    # Path to a file whose presence indicates the live slot is currently
    # rate-limited. Optional — leave default to disable.
    rate_limit_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.subscriptions:
            raise ValueError("Config.subscriptions must be non-empty")
        if not self.primary:
            self.primary = self.subscriptions[0]
        if not self.fallback_order:
            self.fallback_order = [s for s in self.subscriptions if s != self.primary]
        unknown = [s for s in self.fallback_order if s not in self.subscriptions]
        if unknown:
            raise ValueError(
                f"fallback_order references slots not in subscriptions: {unknown}"
            )
        if self.primary not in self.subscriptions:
            raise ValueError(
                f"primary {self.primary!r} not in subscriptions {self.subscriptions}"
            )

    # ---- Derived paths ----

    @property
    def creds_link(self) -> Path:
        return self.creds_dir / self.creds_live_name

    @property
    def switch_log(self) -> Path:
        return self.state_dir / "subscription-switches.log"

    @property
    def rebalance_state_file(self) -> Path:
        return self.state_dir / "subscription-rebalance-state.json"

    @property
    def reset_times_file(self) -> Path:
        return self.state_dir / "subscription-reset-times.json"

    def slot_path(self, sub: str) -> Path:
        return self.creds_dir / self.slot_template.format(sub=sub)
