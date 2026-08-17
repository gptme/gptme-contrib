"""PM NOOP streak detector — state-file-based dispatch back-off.

Tracks consecutive pm-react NOOP dispatch cycles (no items dispatched) and
gates the next dispatch cycle when the streak exceeds a threshold.

State file: ``state/pm-noop-streak.json`` (workspace-relative).
CLI entry point: ``python3 -m gptme_runloops.pm_noop_streak <subcommand>``.

Env vars::

    PM_NOOP_STREAK_BACKOFF_N       — streak length before back-off (default 2)
    PM_NOOP_STREAK_BACKOFF_MINUTES — back-off window in minutes (default 30)
    PM_NOOP_STREAK_DISABLE         — set to "1" to disable the gate entirely

Exit codes (``check`` subcommand):
    0  — proceed with dispatch
    10 — back-off window active, skip dispatch
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # Windows — locking is best-effort
    _fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Default state file path, relative to the workspace root.
DEFAULT_STATE_FILENAME = "state/pm-noop-streak.json"

ENV_BACKOFF_N = "PM_NOOP_STREAK_BACKOFF_N"
ENV_BACKOFF_MINUTES = "PM_NOOP_STREAK_BACKOFF_MINUTES"
ENV_DISABLE = "PM_NOOP_STREAK_DISABLE"

DEFAULT_BACKOFF_N = 2
DEFAULT_BACKOFF_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


class NoopStreakDetector:
    """State-file-based NOOP streak gate for PM dispatch cycles.

    Usage::

        detector = NoopStreakDetector(state_path=Path("state/pm-noop-streak.json"))

        # At start of dispatch cycle:
        skip, _ = detector.should_skip()
        if skip:
            sys.exit("back-off active")

        # At end of dispatch cycle:
        if items_dispatched == 0:
            detector.record_noop()
        else:
            detector.record_success()
    """

    def __init__(
        self,
        state_path: Path | None = None,
        backoff_n: int | None = None,
        backoff_minutes: int | None = None,
    ) -> None:
        if state_path is None:
            # Use the caller's working directory — running from the workspace root
            # is the expected usage, and walking up from __file__ breaks in pip installs
            # (site-packages has no .git, so the walk reaches / and writes there).
            state_path = Path.cwd() / DEFAULT_STATE_FILENAME
        self.state_path = state_path

        if backoff_n is None:
            try:
                backoff_n = int(os.environ.get(ENV_BACKOFF_N, ""))
            except ValueError:
                backoff_n = DEFAULT_BACKOFF_N
        if backoff_minutes is None:
            try:
                backoff_minutes = int(os.environ.get(ENV_BACKOFF_MINUTES, ""))
            except ValueError:
                backoff_minutes = DEFAULT_BACKOFF_MINUTES

        self.backoff_n = backoff_n
        self.backoff_minutes = backoff_minutes

    # --- State I/O ---

    @contextlib.contextmanager
    def _file_lock(self) -> Generator[None, None, None]:
        """Exclusive file lock around a read-modify-write cycle.

        Uses a separate .lock sidecar file so the lock fd is always openable
        even before the state file exists.
        """
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            lf = open(lock_path, "a")
        except OSError as exc:
            logger.warning(
                "Cannot open lock file %s: %s — proceeding without lock", lock_path, exc
            )
            yield
            return
        with lf:
            if _fcntl is not None:
                _fcntl.flock(lf, _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(lf, _fcntl.LOCK_UN)

    def _load(self) -> dict[str, object]:
        try:
            raw = json.loads(self.state_path.read_text())
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict[str, object]) -> None:
        """Atomic write via temp file + os.replace to avoid partial writes."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(state, indent=2, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.state_path.parent,
                delete=False,
                suffix=".tmp",
            ) as tf:
                tf.write(data)
                tmp_path = tf.name
            os.replace(tmp_path, self.state_path)
        except OSError as exc:
            logger.warning("Failed to save noop streak state: %s", exc)

    # --- Public API ---

    def should_skip(self) -> tuple[bool, str]:
        """Return (skip, reason).

        skip=True when the back-off window is active — the caller should abort
        the dispatch cycle.  skip=False otherwise.
        """
        if os.environ.get(ENV_DISABLE, "") == "1":
            return False, f"disabled via {ENV_DISABLE}=1"

        state = self._load()
        _bu = state.get("backoff_until")
        backoff_until_str: str | None = _bu if isinstance(_bu, str) else None
        if not backoff_until_str:
            streak = state.get("streak_count", 0)
            return False, f"no active back-off (streak={streak})"

        backoff_until = _parse_dt(backoff_until_str)
        if backoff_until is None:
            return False, "unparseable backoff_until — proceeding"

        now = _now()
        if now >= backoff_until:
            return False, (
                f"back-off expired at {backoff_until.isoformat()} — allowing retry"
            )

        remaining = int((backoff_until - now).total_seconds() / 60)
        streak = state.get("streak_count", 0)
        return True, (
            f"back-off active: streak={streak}, "
            f"backoff_until={backoff_until.isoformat()}, "
            f"~{remaining}min remaining"
        )

    def record_noop(self) -> dict[str, object]:
        """Record that the dispatch cycle dispatched zero items.

        Increments streak_count.  When streak_count >= backoff_n, sets
        backoff_until = now + backoff_minutes.
        """
        with self._file_lock():
            state = self._load()
            now = _now()
            # If a backoff window just expired, treat it as a fresh start so a
            # single NOOP after the rest window does not immediately re-arm.
            _bu = state.get("backoff_until")
            if _bu and isinstance(_bu, str):
                bu_dt = _parse_dt(_bu)
                if bu_dt is not None and now >= bu_dt:
                    state["streak_count"] = 0
                    state.pop("backoff_until", None)
                    logger.info(
                        "PM NOOP: expired back-off window detected — resetting streak"
                    )
            _sc = state.get("streak_count")
            streak = (int(_sc) if isinstance(_sc, int | float) else 0) + 1

            state["streak_count"] = streak
            state["last_noop_ts"] = now.isoformat()

            if streak >= self.backoff_n:
                backoff_until = now + timedelta(minutes=self.backoff_minutes)
                state["backoff_until"] = backoff_until.isoformat()
                logger.info(
                    "PM NOOP streak=%d/%d — back-off until %s",
                    streak,
                    self.backoff_n,
                    backoff_until.isoformat(),
                )
            else:
                # Don't reset an active back-off window on a sub-threshold NOOP.
                # (The window was set on a prior streak; let it expire naturally.)
                logger.info(
                    "PM NOOP streak=%d/%d — below threshold, no back-off yet",
                    streak,
                    self.backoff_n,
                )

            self._save(state)
        return state

    def record_success(self) -> dict[str, object]:
        """Record that the dispatch cycle dispatched at least one item.

        Resets streak_count to 0 and clears any active backoff_until.
        """
        with self._file_lock():
            state = self._load()
            old_streak = state.get("streak_count", 0)
            state["streak_count"] = 0
            state.pop("backoff_until", None)
            state["last_success_ts"] = _now().isoformat()
            logger.info("PM dispatch success — streak reset from %d to 0", old_streak)
            self._save(state)
        return state

    def status(self) -> dict[str, object]:
        """Return the full current state dict for diagnostic display."""
        state: dict[str, object] = {**self._load()}
        state["backoff_n"] = self.backoff_n
        state["backoff_minutes"] = self.backoff_minutes
        return state


# --- CLI ---


def _check_main(detector: NoopStreakDetector) -> int:
    skip, reason = detector.should_skip()
    prefix = "PM_NOOP_STREAK_SKIP=1" if skip else "PM_NOOP_STREAK_SKIP=0"
    print(f"{prefix} — {reason}", file=sys.stderr)
    return 10 if skip else 0


def _record_noop_main(detector: NoopStreakDetector) -> int:
    state = detector.record_noop()
    streak = state.get("streak_count", 0)
    backoff = state.get("backoff_until", "")
    print(
        f"Recorded NOOP: streak={streak}, backoff_until={backoff or 'none'}",
        file=sys.stderr,
    )
    return 0


def _record_success_main(detector: NoopStreakDetector) -> int:
    state = detector.record_success()
    print(
        f"Recorded success: streak reset, last_success={state.get('last_success_ts', '')}",
        file=sys.stderr,
    )
    return 0


def _status_main(detector: NoopStreakDetector) -> int:
    status = detector.status()
    print(json.dumps(status, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Subcommands::

        check          — exit 0 (proceed) or 10 (skip)
        record-noop    — record a zero-dispatch cycle
        record-success — record a successful dispatch cycle
        status         — print state as JSON
    """
    if argv is None:
        argv = sys.argv[1:]

    import argparse

    parser = argparse.ArgumentParser(
        prog="gptme_runloops.pm_noop_streak",
        description="PM NOOP streak gate for dispatch back-off.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Path to state JSON file (default: state/pm-noop-streak.json in repo root)",
    )
    parser.add_argument(
        "--backoff-n",
        type=int,
        default=None,
        help=f"Streak length before back-off (default: {DEFAULT_BACKOFF_N}, env: {ENV_BACKOFF_N})",
    )
    parser.add_argument(
        "--backoff-minutes",
        type=int,
        default=None,
        help=f"Back-off window in minutes (default: {DEFAULT_BACKOFF_MINUTES}, env: {ENV_BACKOFF_MINUTES})",
    )
    subparsers = parser.add_subparsers(dest="subcmd")
    subparsers.add_parser("check", help="Exit 0 (proceed) or 10 (skip dispatch)")
    subparsers.add_parser("record-noop", help="Record a zero-dispatch cycle")
    subparsers.add_parser("record-success", help="Record a successful dispatch cycle")
    subparsers.add_parser("status", help="Print state as JSON")

    args = parser.parse_args(argv)

    state_path = Path(args.state_file) if args.state_file else None
    detector = NoopStreakDetector(
        state_path=state_path,
        backoff_n=args.backoff_n,
        backoff_minutes=args.backoff_minutes,
    )

    subcmd = args.subcmd or "check"
    if subcmd == "check":
        return _check_main(detector)
    if subcmd == "record-noop":
        return _record_noop_main(detector)
    if subcmd == "record-success":
        return _record_success_main(detector)
    if subcmd == "status":
        return _status_main(detector)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
