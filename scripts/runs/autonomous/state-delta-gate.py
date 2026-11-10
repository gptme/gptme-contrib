#!/usr/bin/env python3
"""State-delta admission gate with persistent streak tracking.

Wraps :func:`gptme_runloops.gates.state_delta_gate` with a JSON state file so
bash callers don't hand-roll the noop streak counter and last-executed
timestamp. The state file tracks the two inputs that drive the wall-clock
backoff and the explore lane:

* ``noop_only_streak`` — consecutive executed clean-noop sessions (not skipped
  ones). Incremented by ``record-noop``, reset to 0 by ``record-productive``.
* ``last_executed_ts`` — ISO-8601 timestamp of the last **executed** session
  (the last time ``record-noop`` or ``record-productive`` was called). Skipped
  timer ticks must NOT call either command.

Exit codes (``check`` subcommand):
    0  — proceed (gate inactive, quota bypass, addressed, backoff, change, or explore)
    10 — skip

Typical integration in ``autonomous-run-cc.sh``::

    # Before the session:
    if ! python3 scripts/state-delta-gate.py check \\
            --addressed "$( ... your addressed-signal probe ... )" \\
            --quota-behind-pace "$QUOTA_BEHIND_PACE"; then
        log "state-delta gate: skip"; exit 0
    fi

    # After the session:
    if [ "$SESSION_RESULT" = "noop" ]; then
        python3 scripts/state-delta-gate.py record-noop
    else
        python3 scripts/state-delta-gate.py record-productive
    fi
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROCEED = 0
SKIP = 10
UTC = timezone.utc

DEFAULT_STATE_FILE = "state/state-delta-gate.json"
DEFAULT_EXPLORE_RATE = 0.05


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _read_state(state_file: Path) -> dict:
    try:
        raw = json.loads(state_file.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=state_file.parent, delete=False, suffix=".tmp"
        ) as tf:
            tf.write(data)
            tmp = tf.name
        os.replace(tmp, state_file)
    except OSError as exc:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print(
            f"state-delta-gate: warning: could not write state: {exc}", file=sys.stderr
        )


def _check(args: argparse.Namespace) -> int:
    try:
        from gptme_runloops.gates import (
            GateDecision,
            explore_roll,
            state_delta_backoff_seconds,
            state_delta_gate,
        )
    except ImportError as exc:
        print(
            f"state-delta-gate: gptme_runloops not installed — {exc}",
            file=sys.stderr,
        )
        return PROCEED  # fail open: missing library must not suppress sessions

    state_file = Path(args.state_file)
    if not state_file.is_absolute():
        state_file = Path(args.workspace).resolve() / state_file

    state = _read_state(state_file)
    now = _now()

    streak_raw = state.get("noop_only_streak", 0)
    noop_only_streak = (
        int(streak_raw) if isinstance(streak_raw, int | float | str) else 0
    )
    last_executed_ts = _parse_dt(state.get("last_executed_ts"))

    backoff_seconds = state_delta_backoff_seconds(noop_only_streak)
    if last_executed_ts is not None and backoff_seconds > 0:
        elapsed = (now - last_executed_ts).total_seconds()
        wall_clock_backoff_expired = elapsed >= backoff_seconds
    else:
        # No timestamp or streak=0 → gate inactive anyway; pass False to let
        # the gate's own inactive branch decide (it won't reach backoff).
        wall_clock_backoff_expired = False

    addressed = (
        args.addressed.lower() in ("1", "true", "yes") if args.addressed else False
    )
    quota_behind_pace = (
        args.quota_behind_pace.lower() in ("1", "true", "yes")
        if args.quota_behind_pace
        else False
    )

    explore_rate = args.explore_rate
    explore = explore_roll(explore_rate)

    decision: GateDecision = state_delta_gate(
        noop_only_streak=noop_only_streak,
        quota_behind_pace=quota_behind_pace,
        addressed_signals=(lambda: True) if addressed else None,
        wall_clock_backoff_expired=wall_clock_backoff_expired,
        force_session=args.force,
        explore=explore,
    )

    if args.verbose:
        print(
            f"state-delta-gate: {'proceed' if decision.proceed else 'skip'} "
            f"(cause={decision.cause}, streak={noop_only_streak}, "
            f"backoff={backoff_seconds}s, elapsed="
            f"{int((now - last_executed_ts).total_seconds()) if last_executed_ts else 'unknown'}s, "
            f"reason={decision.reason!r})",
            file=sys.stderr,
        )
    else:
        print(
            f"GATE_CAUSE={decision.cause} GATE_PROCEED={'1' if decision.proceed else '0'}",
        )

    return PROCEED if decision.proceed else SKIP


def _record_noop(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file)
    if not state_file.is_absolute():
        state_file = Path(args.workspace).resolve() / state_file

    state = _read_state(state_file)
    streak_raw = state.get("noop_only_streak", 0)
    streak = int(streak_raw) if isinstance(streak_raw, int | float | str) else 0
    streak += 1
    now = _now()
    state["noop_only_streak"] = streak
    state["last_executed_ts"] = now.isoformat()
    _write_state(state_file, state)
    print(
        f"state-delta-gate: recorded noop (streak={streak}, ts={now.isoformat()})",
        file=sys.stderr,
    )
    return 0


def _record_productive(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file)
    if not state_file.is_absolute():
        state_file = Path(args.workspace).resolve() / state_file

    state = _read_state(state_file)
    old_streak = state.get("noop_only_streak", 0)
    now = _now()
    state["noop_only_streak"] = 0
    state["last_executed_ts"] = now.isoformat()
    _write_state(state_file, state)
    print(
        f"state-delta-gate: recorded productive (streak reset from {old_streak} to 0, "
        f"ts={now.isoformat()})",
        file=sys.stderr,
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file)
    if not state_file.is_absolute():
        state_file = Path(args.workspace).resolve() / state_file

    state = _read_state(state_file)

    try:
        from gptme_runloops.gates import state_delta_backoff_seconds
    except ImportError:
        state_delta_backoff_seconds = None

    streak_raw = state.get("noop_only_streak", 0)
    streak = int(streak_raw) if isinstance(streak_raw, int | float | str) else 0
    backoff = (
        state_delta_backoff_seconds(streak)
        if state_delta_backoff_seconds
        else "unknown"
    )
    state["_derived_backoff_seconds"] = backoff
    state["_state_file"] = str(state_file)
    print(json.dumps(state, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="state-delta-gate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Workspace root (state file resolved relative to this).",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("STATE_DELTA_GATE_FILE", DEFAULT_STATE_FILE),
        help=f"State JSON file path (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=os.environ.get("STATE_DELTA_GATE_VERBOSE", "") == "1",
        help="Emit human-readable decision to stderr.",
    )

    subparsers = parser.add_subparsers(dest="subcmd", required=True)

    check_p = subparsers.add_parser(
        "check",
        help="Exit 0 (proceed) or 10 (skip). Prints GATE_CAUSE= GATE_PROCEED= on stdout.",
    )
    check_p.add_argument(
        "--addressed",
        default=os.environ.get("GATE_ADDRESSED", ""),
        metavar="BOOL",
        help="1/true/yes when something was addressed to this agent since the last session.",
    )
    check_p.add_argument(
        "--quota-behind-pace",
        default=os.environ.get("GATE_QUOTA_BEHIND_PACE", ""),
        metavar="BOOL",
        help="1/true/yes when quota is behind pace enough to bypass suppression.",
    )
    check_p.add_argument(
        "--force",
        action="store_true",
        default=os.environ.get("FORCE_SESSION", "") in ("1", "true"),
        help="Force session regardless of gate state (manual runs).",
    )
    check_p.add_argument(
        "--explore-rate",
        type=float,
        default=float(os.environ.get("GATE_EXPLORE_RATE", str(DEFAULT_EXPLORE_RATE))),
        help=f"Epsilon-explore rate for sampling would-be skips (default: {DEFAULT_EXPLORE_RATE}).",
    )

    subparsers.add_parser(
        "record-noop",
        help="Record an executed clean-noop session. Increments streak, updates timestamp.",
    )
    subparsers.add_parser(
        "record-productive",
        help="Record a productive session. Resets streak to 0, updates timestamp.",
    )
    subparsers.add_parser("status", help="Print current state as JSON.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcmd == "check":
        return _check(args)
    if args.subcmd == "record-noop":
        return _record_noop(args)
    if args.subcmd == "record-productive":
        return _record_productive(args)
    if args.subcmd == "status":
        return _status(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
