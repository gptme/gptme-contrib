"""SubscriptionManager: orchestrates slot rotation across quota windows.

Ported from Bob's ``scripts/manage-subscription.py`` (gptme/gptme-contrib#831).
All paths and thresholds come from :class:`Config`; nothing here knows
about Bob-specific state directories.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from credential_slots import SlotManager

from gptme_subscription.config import Config
from gptme_subscription.observation import (
    format_duration,
    subscription_pressure_from_usage,
)
from gptme_subscription.observation import (
    is_subscription_blocked as _generic_is_blocked,
)
from gptme_subscription.routing import (
    compute_window_pacing,
)
from gptme_subscription.state import atomic_write_text, locked_state_file

logger = logging.getLogger(__name__)

# ---- Public types ----


@dataclass
class Decision:
    """The recommendation produced by :meth:`SubscriptionManager.evaluate`."""

    active: str | None
    action: str  # "stay" or "switch"
    target: str | None
    reason: str
    mode: str | None = None
    hold_until: str | None = None
    hold_seconds: int | None = None
    weekly_utilization: float | None = None
    five_hour_utilization: float | None = None
    sonnet_weekly_utilization: float | None = None
    pace_overage: float | None = None
    rebalance_trigger: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "active": self.active,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
        }
        for k in (
            "mode",
            "hold_until",
            "hold_seconds",
            "weekly_utilization",
            "five_hour_utilization",
            "sonnet_weekly_utilization",
            "pace_overage",
            "rebalance_trigger",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


# ---- Helpers ----


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- Main class ----


class SubscriptionManager:
    """Quota-aware slot rotation orchestrator.

    Wraps a :class:`credential_slots.SlotManager` for the actual symlink
    flips and layers quota-driven decision logic on top.
    """

    def __init__(self, config: Config):
        self.config = config
        self._last_switch_deferred = False
        self._slot_manager = SlotManager(
            creds_dir=config.creds_dir,
            subscriptions=config.subscriptions,
            slot_template=config.slot_template,
            live_name=config.creds_live_name,
            fingerprint_template=config.fingerprint_template,
            lock_guard=self._lock_guard,
            on_switch=self._log_switch,
        )

    # ---- Lock guard (autonomous sessions) ----

    def _lock_guard(self) -> list[str]:
        pattern = self.config.lock_glob
        if not pattern:
            return []
        active: list[str] = []
        for lock_path in glob.glob(pattern):
            try:
                raw = Path(lock_path).read_text().strip()
                pid = int(raw)
            except (OSError, ValueError):
                continue
            if _pid_is_alive(pid):
                name = Path(lock_path).stem
                active.append(name)
        return sorted(active)

    # ---- Switch log ----

    def _log_switch(self, sub: str, reason: str) -> None:
        log = self.config.switch_log
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log, "a") as f:
            f.write(f"{ts} switched to {sub} — {reason}\n")

    def seconds_since_last_switch_to(self, sub: str) -> int | None:
        log = self.config.switch_log
        if not log.exists():
            return None
        try:
            lines = log.read_text().strip().split("\n")
            for line in reversed(lines):
                if not line.strip():
                    continue
                if f"switched to {sub}" not in line:
                    continue
                ts_str = line.split(" ")[0]
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return int((datetime.now(timezone.utc) - ts).total_seconds())
        except (ValueError, IndexError):
            pass
        return None

    def seconds_since_last_primary_departure(self) -> int | None:
        """Seconds since we last switched AWAY from the primary slot."""
        log = self.config.switch_log
        if not log.exists():
            return None
        primary = self.config.primary
        try:
            previous_sub: str | None = None
            last_departure: datetime | None = None
            for line in log.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                ts_str = line.split(" ")[0]
                match = re.search(r"switched to (\w+)\b", line)
                if match is None:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                current_sub = match.group(1)
                if previous_sub == primary and current_sub != primary:
                    last_departure = ts
                previous_sub = current_sub
            if last_departure is not None:
                return int(
                    (datetime.now(timezone.utc) - last_departure).total_seconds()
                )
        except (OSError, ValueError, IndexError):
            pass
        return None

    def infer_active_slot_from_log(self) -> str | None:
        log = self.config.switch_log
        if not log.exists():
            return None
        try:
            text = log.read_text()
        except OSError:
            return None
        for line in reversed(text.rstrip().split("\n")):
            m = re.search(r"switched to (\w+)\b", line)
            if m and m.group(1) in self.config.subscriptions:
                return m.group(1)
        return None

    # ---- Slot introspection (delegate to credential-slots) ----

    def get_active_subscription(self) -> str | None:
        result: str | None = self._slot_manager.get_active_subscription()
        return result

    def get_available_subscriptions(self) -> list[str]:
        result: list[str] = self._slot_manager.get_available_subscriptions()
        return result

    def slot_path(self, sub: str) -> Path:
        result: Path = self._slot_manager.slot_path(sub)
        return result

    def slot_is_fresh(self, sub: str, grace_seconds: int = 300) -> tuple[bool, str]:
        result: tuple[bool, str] = self._slot_manager.slot_is_fresh(
            sub, grace_seconds=grace_seconds
        )
        return result

    def slot_credential_is_stale(
        self, sub: str, stale_days: float = 7.0, *, now: datetime | None = None
    ) -> tuple[bool, str]:
        """True if the slot's credential file is missing or not refreshed recently.

        A slot is considered stale when its credential file hasn't been written
        in ``stale_days`` days — a healthy in-use slot gets its token rewritten
        regularly by Claude Code. Missing files are always stale. Pure / no
        network calls.
        """
        path = self.config.slot_path(sub)
        current_ts = (now or datetime.now(timezone.utc)).timestamp()
        try:
            mtime = path.stat().st_mtime
            age_days = (current_ts - mtime) / 86400.0
        except OSError:
            return True, "credential file missing"
        if age_days > stale_days:
            return True, f"{age_days:.1f}d old (>{stale_days:.0f}d threshold)"
        return False, f"{age_days:.1f}d old"

    def detect_live_slot_drift(self) -> dict[str, Any] | None:
        drift = self._slot_manager.detect_live_slot_drift()
        return None if drift is None else dict(drift)

    def detect_slot_identity_drift(self, sub: str) -> dict[str, Any]:
        return dict(self._slot_manager.detect_slot_identity_drift(sub))

    def capture_slot_fingerprint(self, sub: str) -> str | None:
        result: str | None = self._slot_manager.capture_slot_fingerprint(sub)
        return result

    # ---- Usage check ----

    def check_usage(
        self, no_cache: bool = False, stale_cache: Path | None = None
    ) -> dict[str, Any] | None:
        """Return usage data, with optional stale-cache fallback on lock contention.

        When the usage script fails (e.g. `/tmp/claude-usage-scrape.lock` held
        by a concurrent ``subscription-usage-history.py`` run), passing
        ``stale_cache`` causes us to fall back to the last-known-good JSON file
        instead of propagating ``None`` to the caller.  The caller decides which
        slot file to use; ``cli._cmd_evaluate`` passes the active-slot path.
        """
        script = self.config.usage_script
        if script is None or not script.exists():
            return None
        cmd = [str(script), "--json"]
        if no_cache:
            cmd.append("--no-cache")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                return dict(json.loads(result.stdout))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        # Stale-cache fallback: prefer a degraded reading over "could not check usage"
        _STALE_MAX_AGE_S = 4 * 3600  # 4 hours — don't route on an ancient snapshot
        if stale_cache is not None and stale_cache.exists():
            try:
                age_s = (
                    datetime.now(tz=timezone.utc).timestamp()
                    - stale_cache.stat().st_mtime
                )
                if age_s > _STALE_MAX_AGE_S:
                    logger.warning(
                        "check_usage: stale cache %s is %.0fh old (max %.0fh) — skipping",
                        stale_cache,
                        age_s / 3600,
                        _STALE_MAX_AGE_S / 3600,
                    )
                else:
                    data = json.loads(stale_cache.read_text())
                    if data.get("_ok"):
                        logger.warning(
                            "check_usage: live probe failed, returning stale cache (age %.0fs)",
                            age_s,
                        )
                        return {**data, "_stale": True}
            except (json.JSONDecodeError, OSError):
                pass
        return None

    # ---- Rate limit ----

    def is_rate_limited(self) -> bool:
        rl = self.config.rate_limit_file
        return bool(rl and rl.exists())

    def clear_rate_limit(self) -> None:
        rl = self.config.rate_limit_file
        if rl is not None:
            rl.unlink(missing_ok=True)

    @staticmethod
    def is_subscription_blocked(
        usage: dict[str, Any], *, config: Config | None = None
    ) -> tuple[bool, str]:
        cfg = config or Config()
        # Reuse generic logic but with thresholds we control.
        return _generic_is_blocked(
            usage,
            weekly_exhausted=cfg.weekly_exhausted,
            five_hour_exhausted=cfg.five_hour_exhausted,
            sonnet_weekly_exhausted=cfg.sonnet_weekly_exhausted,
        )

    # ---- Switch ----

    def switch_to(self, sub: str, reason: str, force: bool = False) -> bool:
        """Flip the live symlink to a named slot. Returns True on success."""
        self._last_switch_deferred = False
        result = self._slot_manager.switch_to(sub, reason, force=force)
        if not result.ok:
            if result.deferred_locks and not force:
                self._last_switch_deferred = True
                return False
            return False
        self.clear_rate_limit()
        return True

    def heal_drift(self, execute: bool = False) -> tuple[bool, str]:
        drift = self._slot_manager.detect_live_slot_drift()
        if drift is None or not drift.get("drift"):
            return False, "no drift"
        target = self.infer_active_slot_from_log()
        if target is None:
            return False, "cannot infer active slot from switch log"
        target_slot = self._slot_manager.slot_path(target)
        if not execute:
            return (
                True,
                f"would heal: copy live → {target_slot.name}, then symlink live → {target_slot.name}",
            )
        result = self._slot_manager.heal_drift_to(target, force=True)
        if result.ok:
            return True, f"healed: synced live → {target_slot.name}, symlink restored"
        return False, result.reason

    @property
    def last_switch_deferred(self) -> bool:
        return self._last_switch_deferred

    # ---- Rebalance state ----

    def load_rebalance_state(
        self, now: datetime | None = None
    ) -> dict[str, Any] | None:
        path = self.config.rebalance_state_file
        current_time = now or datetime.now(timezone.utc)
        with locked_state_file(path):
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    path.unlink(missing_ok=True)
                    return None
                hold_until = datetime.fromisoformat(payload["hold_until"])
                if hold_until <= current_time:
                    path.unlink(missing_ok=True)
                    return None
                payload["hold_until"] = hold_until
                return payload
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                return None

    def save_rebalance_state(self, decision: dict[str, Any]) -> None:
        if decision.get("mode") not in (
            "rebalance",
            "forward-routing",
            "capacity-rebalance",
            "manual-switch",
        ):
            return
        hold_until = decision.get("hold_until")
        if not hold_until:
            return
        if isinstance(hold_until, datetime):
            decision["hold_until"] = hold_until.isoformat()
        path = self.config.rebalance_state_file
        with locked_state_file(path):
            atomic_write_text(path, json.dumps(decision, indent=2) + "\n")

    def clear_rebalance_state(self) -> None:
        path = self.config.rebalance_state_file
        with locked_state_file(path):
            path.unlink(missing_ok=True)

    def record_manual_switch_hold(
        self, target: str, now: datetime | None = None
    ) -> None:
        """Persist a hold protecting an operator's explicit ``--switch``.

        Without this, an automated ``--execute`` (rebalance / forward-routing)
        running seconds later — e.g. at the start of a concurrent autonomous
        session — would re-decide and immediately route away from the slot the
        operator just selected. The hold lasts ``forward_routing_hold_seconds``,
        matching the duration the automated paths use for their own switches.
        """
        current_time = now or datetime.now(timezone.utc)
        hold_seconds = self.config.forward_routing_hold_seconds
        hold_until = current_time + timedelta(seconds=hold_seconds)
        self.save_rebalance_state(
            {
                "active": target,
                "action": "stay",
                "target": target,
                "reason": f"manual switch via --switch {target}",
                "mode": "manual-switch",
                "hold_until": hold_until.isoformat(),
                "hold_seconds": hold_seconds,
            }
        )

    def compute_rebalance_hold_seconds(self, pace_overage: float) -> int:
        cfg = self.config
        if pace_overage <= 0:
            return cfg.rebalance_min_hold
        weekly_window_seconds = 7 * 24 * 3600
        catch_up_seconds = int(
            pace_overage * weekly_window_seconds / cfg.rebalance_target_utilization
        )
        return max(
            cfg.rebalance_min_hold, min(cfg.rebalance_max_hold, catch_up_seconds)
        )

    # ---- Reset-time observation store ----

    def record_sub_reset_time(
        self, sub: str, resets_in_seconds: float, usage: dict[str, Any] | None = None
    ) -> None:
        path = self.config.reset_times_file
        try:
            with locked_state_file(path):
                data: dict[str, Any] = {}
                if path.exists():
                    data = json.loads(path.read_text())
                entry: dict[str, Any] = {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "resets_in_seconds": int(resets_in_seconds),
                }
                if usage is not None:
                    weekly = usage.get("seven_day", {})
                    five_hour = usage.get("five_hour", {})
                    sonnet = usage.get("seven_day_sonnet", {})
                    for key, source_key, source in (
                        ("weekly_utilization", "utilization", weekly),
                        ("five_hour_utilization", "utilization", five_hour),
                        ("sonnet_weekly_utilization", "utilization", sonnet),
                        ("five_hour_resets_in_seconds", "resets_in_seconds", five_hour),
                        ("sonnet_resets_in_seconds", "resets_in_seconds", sonnet),
                    ):
                        value = source.get(source_key)
                        if isinstance(value, int | float):
                            entry[key] = float(value)
                    pressure = subscription_pressure_from_usage(usage)
                    if pressure is not None:
                        entry["pressure"] = round(pressure, 3)
                data[sub] = entry
                atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        except (OSError, json.JSONDecodeError):
            pass

    def _load_sub_observations(self) -> dict[str, dict[str, Any]]:
        path = self.config.reset_times_file
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    @staticmethod
    def _remaining_until_observed_reset(
        entry: dict[str, Any], now: datetime
    ) -> float | None:
        try:
            observed_at = datetime.fromisoformat(str(entry["observed_at"]))
            resets_in = float(entry["resets_in_seconds"])
        except (KeyError, ValueError, TypeError):
            return None
        reset_at = observed_at + timedelta(seconds=resets_in)
        remaining = (reset_at - now).total_seconds()
        return remaining if remaining > 0 else None

    def _pressure_from_observation(
        self, entry: dict[str, Any], now: datetime
    ) -> float | None:
        if self._remaining_until_observed_reset(entry, now) is None:
            return None
        pressure = entry.get("pressure")
        if isinstance(pressure, int | float):
            return float(pressure)
        components = [
            entry.get("weekly_utilization"),
            entry.get("sonnet_weekly_utilization"),
        ]
        five_hour = entry.get("five_hour_utilization")
        five_hour_resets = entry.get("five_hour_resets_in_seconds", 0)
        if isinstance(five_hour_resets, int | float) and five_hour_resets > 7200:
            components.append(five_hour)
        numeric = [float(v) for v in components if isinstance(v, int | float)]
        return max(numeric) if numeric else None

    def capacity_aware_fallback_order(self, now: datetime | None = None) -> list[str]:
        cfg = self.config
        current_time = now or datetime.now(timezone.utc)
        observations = self._load_sub_observations()

        def score(sub: str) -> tuple[float, float]:
            entry = observations.get(sub, {})
            pressure = self._pressure_from_observation(entry, current_time)
            if pressure is None:
                pressure = cfg.unknown_fallback_pressure
            remaining = self._remaining_until_observed_reset(entry, current_time)
            if remaining is None:
                return pressure, float("inf")
            expiry_credit = 0.0
            if remaining < cfg.soon_to_expire_threshold:
                expiry_credit = cfg.expiring_capacity_credit * (
                    1.0 - (remaining / cfg.soon_to_expire_threshold)
                )
            rounded_remaining = round(remaining / 3600) * 3600
            return pressure - expiry_credit, rounded_remaining

        return sorted(cfg.fallback_order, key=score)

    def best_lower_pressure_fallback(
        self, active: str, active_usage: dict[str, Any], now: datetime | None = None
    ) -> tuple[str, float, float] | None:
        cfg = self.config
        current_time = now or datetime.now(timezone.utc)
        active_pressure = subscription_pressure_from_usage(active_usage)
        if (
            active_pressure is None
            or active_pressure < cfg.capacity_rebalance_min_pressure
        ):
            return None
        observations = self._load_sub_observations()
        best: tuple[str, float] | None = None
        for sub in self.capacity_aware_fallback_order(now=current_time):
            if sub == active:
                continue
            pressure = self._pressure_from_observation(
                observations.get(sub, {}), current_time
            )
            if pressure is None:
                continue
            if active_pressure - pressure < cfg.capacity_rebalance_margin:
                continue
            if best is None or pressure < best[1]:
                best = (sub, pressure)
        if best is None:
            return None
        return best[0], active_pressure, best[1]

    # ---- Evaluate ----

    def evaluate(
        self,
        usage: dict[str, Any] | None,
        active: str | None,
        *,
        rebalance_state: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Decision:
        cfg = self.config
        primary = cfg.primary
        current_time = now or datetime.now(timezone.utc)
        d = Decision(
            active=active,
            action="stay",
            target=active,
            reason="quota healthy",
        )

        if usage is None:
            d.reason = "could not check usage"
            return d

        weekly = usage.get("seven_day", {}).get("utilization", 0)
        five_hour = usage.get("five_hour", {}).get("utilization", 0)
        five_hour_resets_in = usage.get("five_hour", {}).get("resets_in_seconds", 0)
        sonnet_weekly = usage.get("seven_day_sonnet", {}).get("utilization", 0)
        sonnet_resets_in = usage.get("seven_day_sonnet", {}).get("resets_in_seconds", 0)

        d.weekly_utilization = weekly
        d.five_hour_utilization = five_hour
        d.sonnet_weekly_utilization = sonnet_weekly

        # Record the live observation for *whichever* slot is active, not just
        # fallbacks. The earlier gate (`active != primary`) was correct for
        # rebalance hold logic but left ``reset-times.json`` permanently empty
        # for the primary slot — downstream readers (Bob's vitals subscription
        # pacing, ``subscription-usage-history.py``) then can't pick up the
        # primary's weekly_utilization between probes.
        _weekly_resets_in = usage.get("seven_day", {}).get("resets_in_seconds")
        if (
            active
            and isinstance(_weekly_resets_in, int | float)
            and _weekly_resets_in > 0
            and not usage.get("_stale")
        ):
            self.record_sub_reset_time(active, float(_weekly_resets_in), usage)

        if active != primary:
            if rebalance_state is not None:
                hold_until = rebalance_state.get("hold_until")
                blocked, _ = self.is_subscription_blocked(usage, config=cfg)
                hold_target = rebalance_state.get("target")
                if (
                    isinstance(hold_until, datetime)
                    and hold_until > current_time
                    and not blocked
                    and hold_target == active
                ):
                    remaining = int((hold_until - current_time).total_seconds())
                    hold_mode = rebalance_state.get("mode", "rebalance")
                    if hold_mode == "forward-routing":
                        hold_reason = rebalance_state.get("reason", "forward routing")
                        d.reason = (
                            f"forward-routing hold active for {format_duration(remaining)} "
                            f"({hold_reason})"
                        )
                        d.mode = "forward-routing-hold"
                    elif hold_mode == "capacity-rebalance":
                        hold_reason = rebalance_state.get(
                            "reason", "capacity rebalance"
                        )
                        d.reason = (
                            f"capacity-rebalance hold active for "
                            f"{format_duration(remaining)} ({hold_reason})"
                        )
                        d.mode = "capacity-rebalance-hold"
                    elif hold_mode == "manual-switch":
                        hold_reason = rebalance_state.get("reason", "manual switch")
                        d.reason = (
                            f"manual-switch hold active for "
                            f"{format_duration(remaining)} ({hold_reason})"
                        )
                        d.mode = "manual-switch-hold"
                    else:
                        raw_pace_overage = rebalance_state.get("pace_overage")
                        pace_overage = (
                            float(raw_pace_overage)
                            if isinstance(raw_pace_overage, int | float)
                            else 0.0
                        )
                        d.reason = (
                            f"rebalance hold active for {format_duration(remaining)} "
                            f"(primary ahead of pace by {pace_overage:.0%})"
                        )
                        d.mode = "rebalance-hold"
                    d.hold_until = hold_until.isoformat()
                    d.hold_seconds = remaining
                    return d

            _fr_blocked, _ = self.is_subscription_blocked(usage, config=cfg)
            if not _fr_blocked and active is not None:
                lower_pressure = self.best_lower_pressure_fallback(
                    active, usage, now=current_time
                )
                if lower_pressure is not None:
                    _target, _active_p, _target_p = lower_pressure
                    _hold_until = current_time + timedelta(
                        seconds=cfg.forward_routing_hold_seconds
                    )
                    d.action = "switch"
                    d.target = _target
                    d.reason = (
                        f"capacity rebalance: {active} pressure {_active_p:.0%}, "
                        f"{_target} pressure {_target_p:.0%}"
                    )
                    d.mode = "capacity-rebalance"
                    d.hold_seconds = cfg.forward_routing_hold_seconds
                    d.hold_until = _hold_until.isoformat()
                    return d

            _resets_in_weekly = usage.get("seven_day", {}).get("resets_in_seconds", 0)
            if (
                not _fr_blocked
                and isinstance(_resets_in_weekly, int | float)
                and _resets_in_weekly > 0
            ):
                _weekly_period_s = 7 * 24 * 3600
                _period_elapsed_s = _weekly_period_s - float(_resets_in_weekly)
                _period_elapsed_frac = _period_elapsed_s / _weekly_period_s
                _idle_threshold_s = _period_elapsed_s * cfg.forward_routing_idle_frac
                if float(_resets_in_weekly) < cfg.soon_to_expire_threshold:
                    _resets_h = int(float(_resets_in_weekly)) // 3600
                    d.reason = (
                        f"staying on {active}: period resets in {_resets_h}h "
                        f"(< {cfg.soon_to_expire_threshold // 3600}h) — "
                        f"consuming soon-to-expire capacity"
                    )
                    d.mode = "soon-to-expire"
                    return d
                if _period_elapsed_frac >= cfg.forward_routing_period_threshold:
                    for _next_sub in cfg.fallback_order:
                        if _next_sub == active:
                            continue
                        _last_s = self.seconds_since_last_switch_to(_next_sub)
                        if _last_s is None or _last_s > _idle_threshold_s:
                            _hold_until = current_time + timedelta(
                                seconds=cfg.forward_routing_hold_seconds
                            )
                            _last_s_str = (
                                f"{format_duration(_last_s)} ago"
                                if _last_s is not None
                                else "never this period"
                            )
                            d.action = "switch"
                            d.target = _next_sub
                            d.reason = (
                                f"forward routing chain: {active} hold expired, "
                                f"{_next_sub} last used {_last_s_str}"
                            )
                            d.mode = "forward-routing"
                            d.hold_seconds = cfg.forward_routing_hold_seconds
                            d.hold_until = _hold_until.isoformat()
                            return d

            d.action = "switch"
            d.target = primary
            fallback_status = (
                f"{weekly:.0%} weekly, {five_hour:.0%} 5h, {sonnet_weekly:.0%} Sonnet"
            )
            d.reason = f"on fallback ({active}) [{fallback_status}] — probe {primary}"
            return d

        # On primary
        exhausted = False
        if weekly >= cfg.weekly_exhausted:
            exhausted = True
            d.reason = f"weekly at {weekly:.0%} (≥{cfg.weekly_exhausted:.0%})"
        elif five_hour >= cfg.five_hour_exhausted and five_hour_resets_in > 7200:
            exhausted = True
            d.reason = (
                f"5h at {five_hour:.0%} (≥{cfg.five_hour_exhausted:.0%}), "
                f"resets in {five_hour_resets_in // 60}m (>2h)"
            )
        elif sonnet_weekly >= cfg.sonnet_weekly_exhausted:
            exhausted = True
            resets_h = sonnet_resets_in // 3600
            d.reason = (
                f"Sonnet weekly at {sonnet_weekly:.0%} "
                f"(≥{cfg.sonnet_weekly_exhausted:.0%}), resets in {resets_h}h"
            )
        elif self.is_rate_limited():
            exhausted = True
            d.reason = "rate limit block file present"

        if exhausted:
            if cfg.fallback_order:
                urgency_order = self.capacity_aware_fallback_order(now=current_time)
                fresh_order: list[str] = []
                stale_msgs: list[str] = []
                for s in urgency_order:
                    stale, msg = self.slot_credential_is_stale(s, now=current_time)
                    if stale:
                        stale_msgs.append(f"{s}: {msg}")
                    else:
                        fresh_order.append(s)
                if fresh_order:
                    d.action = "switch"
                    d.target = fresh_order[0]
                    urgency_top = urgency_order[0]
                    if urgency_top not in fresh_order:
                        # urgency top-pick was stale; staleness drove the selection
                        d.reason += (
                            f" → {urgency_top} stale; selecting {fresh_order[0]}"
                        )
                    elif urgency_top != cfg.fallback_order[0]:
                        # purely urgency-driven reordering (no stale filtering)
                        d.reason += (
                            f" → preferring {fresh_order[0]} "
                            f"(lower pressure / expiry-aware)"
                        )
                else:
                    d.action = "stay"
                    d.target = active
                    d.reason = (
                        f"{d.reason} — all fallbacks stale "
                        f"({'; '.join(stale_msgs)}); reauth needed before switching"
                    )
            else:
                d.reason += " — no fallback defined"
            return d

        # Respect an operator manual-switch hold on a healthy primary too: don't
        # proactively rebalance / forward-route away from an explicit operator
        # choice. (The exhaustion fallback above already fired if needed, so we
        # never strand on an exhausted primary.)
        if rebalance_state is not None:
            manual_hold_until = rebalance_state.get("hold_until")
            manual_hold_target = rebalance_state.get("target")
            if (
                rebalance_state.get("mode") == "manual-switch"
                and isinstance(manual_hold_until, datetime)
                and manual_hold_until > current_time
                and manual_hold_target == active
            ):
                remaining = int((manual_hold_until - current_time).total_seconds())
                hold_reason = rebalance_state.get("reason", "manual switch")
                d.reason = (
                    f"manual-switch hold active for "
                    f"{format_duration(remaining)} ({hold_reason})"
                )
                d.mode = "manual-switch-hold"
                d.hold_until = manual_hold_until.isoformat()
                d.hold_seconds = remaining
                return d

        # Healthy primary — check pacing for rebalance / forward-routing.
        rebalance_stale_note: str | None = None
        candidates: list[tuple[str, float, float]] = []
        pacing = usage.get("_pacing", {})
        actual_overall = float(pacing.get("actual_utilization", weekly))
        target_overall = pacing.get("target_utilization")
        if (
            isinstance(target_overall, int | float)
            and actual_overall - float(target_overall) >= cfg.rebalance_ahead_threshold
            and pacing.get("status") == "overusing"
        ):
            candidates.append(("weekly", actual_overall, float(target_overall)))

        weekly_window_seconds = 7 * 24 * 3600
        sonnet_pacing = compute_window_pacing(
            sonnet_weekly, sonnet_resets_in, weekly_window_seconds
        )
        if sonnet_pacing is not None:
            sonnet_elapsed_frac, sonnet_gap, sonnet_status = sonnet_pacing
            if (
                sonnet_gap >= cfg.rebalance_ahead_threshold
                and sonnet_status == "overusing"
            ):
                candidates.append(("sonnet weekly", sonnet_weekly, sonnet_elapsed_frac))

        if candidates:
            label, actual_util, target_util = max(candidates, key=lambda c: c[1] - c[2])
            pace_overage = actual_util - target_util
            hold_seconds = self.compute_rebalance_hold_seconds(pace_overage)
            hold_until = current_time + timedelta(seconds=hold_seconds)
            urgency_order = self.capacity_aware_fallback_order(now=current_time)
            fresh_rebalance = [
                s
                for s in urgency_order
                if not self.slot_credential_is_stale(s, now=current_time)[0]
            ]
            if fresh_rebalance:
                d.action = "switch"
                d.target = fresh_rebalance[0]
                d.reason = (
                    f"rebalance: primary ahead of {label} pace by "
                    f"{pace_overage:.0%} "
                    f"({actual_util:.0%} vs target {target_util:.0%})"
                )
                d.mode = "rebalance"
                d.hold_seconds = hold_seconds
                d.hold_until = hold_until.isoformat()
                d.pace_overage = round(pace_overage, 3)
                d.rebalance_trigger = label
                return d
            else:
                rebalance_stale_note = (
                    "rebalance skipped — all fallbacks stale, reauth needed"
                )

        # Forward routing on healthy primary
        resets_in_weekly = usage.get("seven_day", {}).get("resets_in_seconds", 0)
        if isinstance(resets_in_weekly, int | float) and resets_in_weekly > 0:
            weekly_period_seconds = 7 * 24 * 3600
            period_elapsed_s = weekly_period_seconds - float(resets_in_weekly)
            period_elapsed_frac = period_elapsed_s / weekly_period_seconds
            idle_threshold_s = period_elapsed_s * cfg.forward_routing_idle_frac
            if (
                period_elapsed_frac >= cfg.forward_routing_period_threshold
                and cfg.fallback_order
            ):
                for sub in cfg.fallback_order:
                    if self.slot_credential_is_stale(sub, now=current_time)[0]:
                        continue  # skip stale slots in forward routing
                    last_s = self.seconds_since_last_switch_to(sub)
                    if last_s is None or last_s > idle_threshold_s:
                        hold_until_ts = current_time + timedelta(
                            seconds=cfg.forward_routing_hold_seconds
                        )
                        last_s_str = (
                            f"{format_duration(last_s)} ago"
                            if last_s is not None
                            else "never this period"
                        )
                        d.action = "switch"
                        d.target = sub
                        d.reason = (
                            f"forward routing: period {period_elapsed_frac:.0%} elapsed, "
                            f"{sub} last used {last_s_str}"
                        )
                        d.mode = "forward-routing"
                        d.hold_seconds = cfg.forward_routing_hold_seconds
                        d.hold_until = hold_until_ts.isoformat()
                        return d

        d.reason = (
            f"primary quota healthy: {weekly:.0%} weekly, "
            f"{five_hour:.0%} 5h, {sonnet_weekly:.0%} Sonnet"
        )
        if rebalance_stale_note:
            d.reason += f"; {rebalance_stale_note}"
        return d

    # ---- External-switch detection (logs out-of-band symlink flips) ----

    def detect_external_switch(self) -> None:
        active = self.get_active_subscription()
        if active is None:
            return
        log = self.config.switch_log
        if not log.exists():
            self._log_switch(active, "initial state detected (no prior log)")
            return
        try:
            lines = log.read_text().strip().split("\n")
            last_line = ""
            for line in reversed(lines):
                if line.strip():
                    last_line = line
                    break
            if not last_line:
                return
            if f"switched to {active}" not in last_line:
                self._log_switch(
                    active, "external switch detected (symlink changed outside script)"
                )
        except (OSError, ValueError, IndexError):
            pass
