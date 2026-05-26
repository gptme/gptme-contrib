"""Command-line interface for ``gptme-subscription``.

Drives :class:`gptme_subscription.manager.SubscriptionManager`. All paths
default to XDG locations but can be overridden via env vars or CLI flags
(see ``gptme-subscription --help``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gptme_subscription.auth import (
    check_credential_file,
    format_reauth_instructions,
    probe_credential,
)
from gptme_subscription.config import Config
from gptme_subscription.manager import SubscriptionManager

PROG = "gptme-subscription"

EPILOG = """\
Configuration sources (highest priority wins):
  1. CLI flags (--slot, --state-dir, ...)
  2. Environment variables:
        GPTME_SUBSCRIPTION_SLOTS              comma-separated, e.g. "bob,alice,erik"
        GPTME_SUBSCRIPTION_FALLBACK_ORDER     comma-separated, e.g. "alice,erik"
        GPTME_SUBSCRIPTION_CREDS_DIR          default: ~/.claude
        GPTME_SUBSCRIPTION_STATE_DIR          default: $XDG_STATE_HOME/gptme-subscription
        GPTME_SUBSCRIPTION_USAGE_SCRIPT       path to check-claude-usage.sh-style probe
        GPTME_SUBSCRIPTION_LOCK_GLOB          glob for autonomous-session lock files
        GPTME_SUBSCRIPTION_WEEKLY_EXHAUSTED   default: 0.85
        GPTME_SUBSCRIPTION_PROBE_COOLDOWN     seconds, default: 1800
  3. XDG defaults

Re-authenticating an expired slot
---------------------------------
If `check-auth --probe` reports a slot as broken (probe fails or status
is missing/malformed), run:

    gptme-subscription --reauth-instructions <sub>

…and follow the printed steps. The OAuth refresh token can also silently
expire on the server side; only the --probe variant catches that.
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Manage Claude Code subscription slots: check quota, switch the "
            "live credential symlink, and detect expired auth tokens."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )

    # Config overrides
    p.add_argument(
        "--slots",
        help="Comma-separated slot names. Overrides $GPTME_SUBSCRIPTION_SLOTS.",
    )
    p.add_argument(
        "--fallback-order",
        help="Comma-separated fallback order. Default: all slots except primary.",
    )
    p.add_argument(
        "--primary",
        help="Slot to treat as primary. Default: first entry in --slots.",
    )
    p.add_argument(
        "--creds-dir",
        type=Path,
        help="Directory holding .credentials.json.* files. Default: ~/.claude.",
    )
    p.add_argument(
        "--state-dir",
        type=Path,
        help="Directory for switch logs and rebalance state.",
    )
    p.add_argument(
        "--usage-script",
        type=Path,
        help="Path to a JSON-emitting usage probe script.",
    )
    p.add_argument(
        "--lock-glob",
        help="Glob for autonomous-session lock files (defers automated switches).",
    )
    p.add_argument(
        "--rate-limit-file",
        type=Path,
        help="Optional file whose presence indicates the live slot is rate-limited.",
    )

    # Modes / actions
    p.add_argument(
        "--status",
        action="store_true",
        help="Print active slot + available slots and exit.",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually apply the recommended switch (default: preview only).",
    )
    p.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Force preview-only even if --execute is also given.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    p.add_argument(
        "--switch",
        metavar="SLOT",
        help="Force-switch to the named slot (preview unless --execute).",
    )
    p.add_argument(
        "--check-identity",
        action="store_true",
        help=(
            "Scan every available slot for refresh-token fingerprint drift. "
            "Exits 0 (clean), 1 (drift), 2 (no baseline to compare)."
        ),
    )
    p.add_argument(
        "--baseline-identity",
        metavar="SLOT",
        help="Capture the current refresh-token fingerprint for a slot.",
    )
    p.add_argument(
        "--heal-drift",
        action="store_true",
        help="Attempt to repair a drifted symlink (preview unless --execute).",
    )

    # Subcommands embedded as flags for simplicity
    p.add_argument(
        "--check-auth",
        action="store_true",
        help="Check credential expiry for every slot.",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="With --check-auth: actually probe each slot (slower, more accurate).",
    )
    p.add_argument(
        "--reauth-instructions",
        metavar="SLOT",
        help="Print re-auth steps for the named slot and exit.",
    )

    return p


def _config_from_args(args: argparse.Namespace) -> Config:
    """Build a Config, with CLI flags overriding env-var defaults."""
    overrides: dict[str, object] = {}
    if args.slots:
        overrides["subscriptions"] = [
            s.strip() for s in args.slots.split(",") if s.strip()
        ]
    if args.fallback_order:
        overrides["fallback_order"] = [
            s.strip() for s in args.fallback_order.split(",") if s.strip()
        ]
    if args.primary:
        overrides["primary"] = args.primary
    if args.creds_dir:
        overrides["creds_dir"] = args.creds_dir
    if args.state_dir:
        overrides["state_dir"] = args.state_dir
    if args.usage_script:
        overrides["usage_script"] = args.usage_script
    if args.lock_glob is not None:
        overrides["lock_glob"] = args.lock_glob
    if args.rate_limit_file:
        overrides["rate_limit_file"] = args.rate_limit_file
    return Config(**overrides)  # type: ignore[arg-type]


# ---- Subcommands ----


def _cmd_check_auth(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    cfg = sm.config
    reports = []
    any_bad = False
    for sub in cfg.subscriptions:
        info = check_credential_file(cfg.slot_path(sub), sub)
        probe_ok: bool | None = None
        probe_msg = ""
        if args.probe:
            info, probe_ok, probe_msg = probe_credential(
                cfg.slot_path(sub), sub, usage_script=cfg.usage_script
            )
        entry = info.to_dict()
        if args.probe:
            entry["probe_ok"] = probe_ok
            entry["probe_message"] = probe_msg
        reports.append(entry)
        if info.needs_reauth_hint or (args.probe and probe_ok is False):
            any_bad = True

    if args.json:
        print(json.dumps({"reports": reports, "any_bad": any_bad}, indent=2))
        return 1 if any_bad else 0

    for entry in reports:
        sub = str(entry["sub"])
        status = str(entry["status"])
        line = f"  [{status:>9}] {sub}"
        if "expires_in_seconds" in entry:
            delta_s = int(entry["expires_in_seconds"])  # type: ignore[call-overload]
            if delta_s >= 0:
                line += f"  (access token valid for {delta_s // 60}m)"
            else:
                line += (
                    f"  (access token lapsed {-delta_s // 60}m ago — "
                    "will refresh on next use)"
                )
        if entry.get("subscription_type"):
            line += f"  [{entry['subscription_type']}]"
        if entry.get("error"):
            line += f"  ERROR: {entry['error']}"
        if "probe_ok" in entry:
            line += f"  probe={'ok' if entry['probe_ok'] else 'FAIL'}"
            if entry.get("probe_message") and not entry["probe_ok"]:
                line += f" ({entry['probe_message']})"
        print(line)

    if any_bad:
        print(
            "\nOne or more slots need attention. Run "
            f"`{PROG} --reauth-instructions <slot>` for the recovery steps.",
            file=sys.stderr,
        )
    return 1 if any_bad else 0


def _cmd_reauth_instructions(args: argparse.Namespace) -> int:
    print(format_reauth_instructions(args.reauth_instructions))
    return 0


def _cmd_status(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    active = sm.get_active_subscription()
    available = sm.get_available_subscriptions()
    cfg = sm.config
    if args.json:
        print(
            json.dumps(
                {
                    "active": active,
                    "available": available,
                    "primary": cfg.primary,
                    "subscriptions": cfg.subscriptions,
                    "creds_link": str(cfg.creds_link),
                },
                indent=2,
            )
        )
        return 0
    print(f"Active slot:   {active}")
    print(f"Primary:       {cfg.primary}")
    print(f"Available:     {', '.join(available)}")
    target = cfg.creds_link.resolve() if cfg.creds_link.is_symlink() else "N/A"
    print(f"Symlink:       {cfg.creds_link} → {target}")
    if cfg.rate_limit_file:
        print(
            f"Rate limited:  {'YES' if cfg.rate_limit_file.exists() else 'no'} "
            f"({cfg.rate_limit_file})"
        )
    return 0


def _cmd_check_identity(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    reports = []
    any_drift = False
    any_baseline = False
    for sub in sm.get_available_subscriptions():
        info = sm.detect_slot_identity_drift(sub)
        reports.append(info)
        if info.get("stored_fingerprint") is not None:
            any_baseline = True
            if info.get("drift"):
                any_drift = True
    if args.json:
        print(json.dumps({"reports": reports, "drift": any_drift}, indent=2))
    else:
        for info in reports:
            marker = "DRIFT" if info.get("drift") else "ok   "
            print(f"  [{marker}] {info['sub']}: {info['reason']}")
    if not any_baseline:
        return 2
    return 1 if any_drift else 0


def _cmd_baseline_identity(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    sub = args.baseline_identity
    if sub not in sm.config.subscriptions:
        print(f"Unknown subscription: {sub}", file=sys.stderr)
        return 1
    fp = sm.capture_slot_fingerprint(sub)
    if args.json:
        print(json.dumps({"sub": sub, "fingerprint": fp}))
        return 0 if fp else 1
    if fp is None:
        print(
            f"  ERROR: could not capture fingerprint for {sub} "
            "(slot missing or has no refresh token)",
            file=sys.stderr,
        )
        return 1
    print(f"  Captured fingerprint for {sub}: {fp[:16]}…")
    return 0


def _cmd_heal_drift(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    healed, reason = sm.heal_drift(execute=args.execute)
    if args.json:
        print(
            json.dumps({"healed": healed, "reason": reason, "executed": args.execute})
        )
    else:
        prefix = "[heal]" if args.execute else "[heal dry-run]"
        print(f"{prefix} {reason}")
    return 0 if healed or "no drift" in reason else 1


def _cmd_switch(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    target = args.switch
    if target not in sm.config.subscriptions:
        print(f"Unknown subscription: {target}", file=sys.stderr)
        return 1
    active = sm.get_active_subscription()
    if not args.execute:
        print(f"[dry-run] would switch to {target}")
        print(f"  current: {active}")
        print("  use --execute to apply")
        return 0
    ok = sm.switch_to(target, f"manual switch via --switch {target}", force=True)
    if ok:
        # Protect the operator's explicit choice: write a manual-switch hold so a
        # concurrent automated --execute doesn't immediately rebalance / forward-
        # route away from it. Previously this cleared all hold state, leaving the
        # manual switch unprotected for the next decision.
        sm.record_manual_switch_hold(target)
        print(f"Switched to {target}")
        sm.check_usage(no_cache=True)
        return 0
    return 1


def _execute_switch_decision(
    sm: SubscriptionManager,
    decision,
    previous: str | None,
    *,
    emit_text: bool,
) -> tuple[int, dict[str, object]]:
    cfg = sm.config
    target = decision.target
    payload: dict[str, object] = {"executed": False}

    if not target:
        payload["reason"] = "malformed switch decision"
        if emit_text:
            print("Malformed switch decision", file=sys.stderr)
        return 1, payload

    def _attempt_revert(previous_slot: str, reason: str) -> bool:
        reverted = sm.switch_to(previous_slot, reason)
        if reverted:
            payload["reverted_to"] = previous_slot
            return True
        payload["revert_failed"] = True
        if sm.last_switch_deferred:
            payload["revert_deferred"] = True
            payload["revert_reason"] = "revert deferred by active locks"
        else:
            payload["revert_reason"] = "revert failed"
        return False

    if target == cfg.primary and previous and previous != cfg.primary:
        cooldown = sm.seconds_since_last_primary_departure()
        if cooldown is not None and cooldown < cfg.probe_primary_cooldown:
            remaining = cfg.probe_primary_cooldown - cooldown
            reason = (
                f"cooldown active: switched away from {cfg.primary} {cooldown}s ago; "
                f"wait {remaining}s before probing again"
            )
            payload["deferred"] = True
            payload["reason"] = reason
            if emit_text:
                print(
                    f"  Cooldown: switched away from {cfg.primary} {cooldown}s ago "
                    f"(<{cfg.probe_primary_cooldown}s). Waiting {remaining}s before probing."
                )
            return 0, payload

    ok = sm.switch_to(target, decision.reason)
    if not ok:
        if sm.last_switch_deferred:
            payload["deferred"] = True
            payload["reason"] = "switch deferred by active locks"
            return 0, payload
        payload["reason"] = "switch refused"
        # Refused switch (not deferred) is a capacity signal — emit a visible
        # alert so a stale or broken fallback slot doesn't silently strand the
        # system on the exhausted primary. See the 2026-05-22 incident.
        if emit_text:
            print(
                f"\n[ALERT] switch to {target!r} refused — credential check failed. "
                f"Reauth: gptme-subscription --reauth-instructions {target}",
                file=sys.stderr,
            )
        return 1, payload

    payload["executed"] = True
    if decision.mode in ("rebalance", "forward-routing", "capacity-rebalance"):
        sm.save_rebalance_state(decision.to_dict())
    else:
        sm.clear_rebalance_state()

    if emit_text:
        print(f"\n→ Switched to {target}")

    new_usage = sm.check_usage(no_cache=True)
    payload["post_switch_usage"] = new_usage
    if not new_usage:
        payload["verified"] = False
        if target == cfg.primary and previous and previous != cfg.primary:
            if emit_text:
                print(f"  WARNING: could not verify {cfg.primary}'s quota — reverting")
            _attempt_revert(previous, "auto-revert: usage check failed")
        return 0, payload

    payload["verified"] = True
    w = new_usage.get("seven_day", {}).get("utilization", 0)
    f5 = new_usage.get("five_hour", {}).get("utilization", 0)
    s = new_usage.get("seven_day_sonnet", {}).get("utilization", 0)
    if emit_text:
        print(f"  New usage: {w:.0%} weekly, {f5:.0%} 5h, {s:.0%} Sonnet")
    weekly_resets = new_usage.get("seven_day", {}).get("resets_in_seconds")
    if (
        target != cfg.primary
        and isinstance(weekly_resets, int | float)
        and weekly_resets > 0
    ):
        sm.record_sub_reset_time(target, float(weekly_resets), new_usage)

    if target == cfg.primary and previous and previous != cfg.primary:
        blocked, reason = sm.is_subscription_blocked(new_usage, config=cfg)
        payload["verification_reason"] = reason
        if blocked:
            if emit_text:
                print(f"  {cfg.primary} blocked: {reason}")
                print(f"  Reverting to {previous}")
            reverted = _attempt_revert(previous, f"auto-revert: {reason}")
            sm.check_usage(no_cache=True)
            if reverted and cfg.rate_limit_file:
                cfg.rate_limit_file.unlink(missing_ok=True)
        else:
            sm.clear_rebalance_state()
            if emit_text:
                print(f"  {cfg.primary} healthy: {reason}")
    elif (
        decision.mode in ("forward-routing", "capacity-rebalance")
        and target != cfg.primary
        and previous
        and previous != target
    ):
        blocked, reason = sm.is_subscription_blocked(new_usage, config=cfg)
        payload["verification_reason"] = reason
        if blocked:
            if emit_text:
                print(f"  {target} already blocked: {reason}")
                print(f"  Reverting to {previous}")
            sm.clear_rebalance_state()
            _attempt_revert(previous, f"auto-revert routing: {target} blocked")
            sm.check_usage(no_cache=True)
        else:
            if emit_text:
                print(f"  {target} healthy for routing: {reason}")
    return 0, payload


def _cmd_evaluate(args: argparse.Namespace, sm: SubscriptionManager) -> int:
    """Default behavior: print recommendation, optionally apply with --execute."""
    sm.detect_external_switch()

    active = sm.get_active_subscription()
    usage = sm.check_usage()
    rebalance_state = sm.load_rebalance_state()
    decision = sm.evaluate(usage, active, rebalance_state=rebalance_state)
    decision_dict = decision.to_dict()

    if args.json:
        if decision.action == "switch" and args.execute:
            rc, payload = _execute_switch_decision(
                sm, decision, active, emit_text=False
            )
            print(json.dumps({**decision_dict, **payload}, indent=2))
            return rc
        print(json.dumps(decision_dict, indent=2))
        return 0

    print(f"Active:   {active}")
    print(f"Decision: {decision.action}")
    if decision.weekly_utilization is not None:
        print(
            f"Usage:    {decision.weekly_utilization:.0%} weekly, "
            f"{decision.five_hour_utilization or 0:.0%} 5h, "
            f"{decision.sonnet_weekly_utilization or 0:.0%} Sonnet"
        )
    print(f"Reason:   {decision.reason}")

    if decision.action != "switch":
        return 0

    target = decision.target
    if not target:
        print("Malformed switch decision", file=sys.stderr)
        return 1

    if not args.execute:
        prefix = "[dry-run] " if args.dry_run else ""
        print(f"\n→ {prefix}Would switch to {target} (use --execute to apply)")
        return 0

    rc, _ = _execute_switch_decision(sm, decision, active, emit_text=True)
    return rc


# ---- Entry point ----


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --dry-run always wins over --execute
    if args.dry_run:
        args.execute = False

    # reauth-instructions doesn't need any state — print and bail
    if args.reauth_instructions:
        return _cmd_reauth_instructions(args)

    try:
        config = _config_from_args(args)
    except ValueError as exc:
        print(f"{PROG}: configuration error: {exc}", file=sys.stderr)
        return 2

    sm = SubscriptionManager(config)

    if args.check_auth:
        return _cmd_check_auth(args, sm)
    if args.check_identity:
        return _cmd_check_identity(args, sm)
    if args.baseline_identity:
        return _cmd_baseline_identity(args, sm)
    if args.heal_drift:
        return _cmd_heal_drift(args, sm)
    if args.status:
        return _cmd_status(args, sm)
    if args.switch:
        return _cmd_switch(args, sm)
    return _cmd_evaluate(args, sm)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
