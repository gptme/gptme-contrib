"""Project monitoring dispatch primitives.

Generic dispatch logic extracted from the bash project-monitoring system:
- Lane classification (fast vs slow partitions)
- Dispatch telemetry ledger
- Slot/concurrency management

These classes model the bash dispatch logic and are used both for testing
and as the design spec for future Python-native dispatch. The bash scripts
remain the primary dispatch path (see project-monitoring-upstream-overhaul).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Slow-lane item types — items that need deep investigation (PR reviews,
# CI diagnostics, merge conflicts, Greptile issues). Fast lane is anything
# else (notifications, assigned issues).
SLOW_LANE_TYPES: set[str] = {
    "pr_update",
    "ci_failure",
    "master_ci_failure",
    "merge_conflict",
    "greptile_needs_fix",
    "greptile_needs_improvement",
}

# Default slot cap for concurrent dispatch workers
DEFAULT_SLOT_CAP = 3

# Default number of fast-lane burst slots above cap
DEFAULT_FAST_BURST_ALLOWANCE = 1

logger = logging.getLogger(__name__)


# --- Data classes ---


@dataclass
class SlotItem:
    """A grouped work item eligible for slot-based dispatch.

    Mirrors a single JSONL line from the grouped_items.jsonl file.
    """

    repo: str
    number: int | None
    types: list[str]
    title: str = ""
    url: str = ""


@dataclass
class LedgerEntry:
    """A single dispatch ledger entry (one JSONL line)."""

    timestamp: str
    phase: str
    lane: str
    dispatch_id: str
    unit_name: str
    item_refs: list[str]
    running_units: int | None = None
    cap: int | None = None
    note: str | None = None
    successes: int | None = None
    failures: int | None = None
    duration_seconds: int | None = None

    @classmethod
    def now(
        cls,
        phase: str,
        lane: str,
        dispatch_id: str,
        unit_name: str,
        item_refs: list[str] | None = None,
        **kwargs: Any,
    ) -> LedgerEntry:
        """Create an entry with auto-timestamp."""
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            phase=phase,
            lane=lane,
            dispatch_id=dispatch_id,
            unit_name=unit_name,
            item_refs=item_refs or [],
            **{
                k: v
                for k, v in kwargs.items()
                if k in {f.name for f in fields(cls)} and v is not None
            },
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "phase": self.phase,
            "lane": self.lane,
            "dispatch_id": self.dispatch_id,
        }
        if self.unit_name:
            result["unit_name"] = self.unit_name
        if self.item_refs is not None:
            result["item_refs"] = self.item_refs
        if self.running_units is not None:
            result["running_units"] = self.running_units
        if self.cap is not None:
            result["cap"] = self.cap
        if self.note:
            result["note"] = self.note
        if self.successes is not None:
            result["successes"] = self.successes
        if self.failures is not None:
            result["failures"] = self.failures
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


# --- Slot key derivation ---

_ISSUE_REF_RE = re.compile(r"^[^/]+/[^#]+#\d+$")


def derive_slot_key(repo: str, number: int | None, types: list[str]) -> str:
    """Derive a slot key from a work item.

    Matches the bash ``derive_slot_key()`` logic:
      - master_ci_failure types → ``{repo}#master-ci``
      - others → ``{repo}#{number}``
    """
    if "master_ci_failure" in types:
        return f"{repo}#master-ci"
    if number is not None:
        return f"{repo}#{number}"
    return f"{repo}#unknown"


# --- Lane classification ---


def classify_lane(types: list[str]) -> str:
    """Classify an item as ``"fast"`` or ``"slow"`` lane.

    Matches the bash ``partition_grouped_items_by_lane()`` logic.
    """
    if any(t in SLOW_LANE_TYPES for t in types):
        return "slow"
    return "fast"


def partition_items(items: list[SlotItem]) -> tuple[list[SlotItem], list[SlotItem]]:
    """Partition items into (fast_items, slow_items)."""
    fast: list[SlotItem] = []
    slow: list[SlotItem] = []
    for item in items:
        if classify_lane(item.types) == "fast":
            fast.append(item)
        else:
            slow.append(item)
    return fast, slow


# --- DispatchLedger ---


class DispatchLedger:
    """Append-only JSONL telemetry for dispatch events.

    Mirrors the bash ``append_dispatch_ledger()`` function.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: LedgerEntry) -> None:
        """Append a single ledger entry to the JSONL file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry.to_dict(), default=str) + "\n")

    def read(self) -> list[LedgerEntry]:
        """Read all entries from the ledger file."""
        if not self.path.exists():
            return []
        entries: list[LedgerEntry] = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(LedgerEntry(**data))
        return entries

    def clear(self) -> None:
        """Clear the ledger (for testing)."""
        if self.path.exists():
            self.path.unlink()


# --- LaneDispatcher ---


@dataclass
class LaneDispatcher:
    """Orchestrates lane-aware slot dispatch for project monitoring items.

    Partitions items into fast/slow lanes, then iterates through each lane
    launching transient systemd units while respecting the global slot cap
    and fast-lane burst allowance.

    This is the Python-equivalent of the bash ``dispatch_items()`` function.
    The bash path is still the primary runtime; this class provides the
    testable design spec and foundation for future Python-native dispatch.

    Attributes:
        slot_manager: SlotManager for capacity checks
        dispatch_callback: Callable for launching units (injectable for tests).
            Receives (slot_unit, slot_key, lane, item, backend, model, script_path).
            Must return a bool (True = launched).
    """

    slot_manager: SlotManager = field(default_factory=lambda: SlotManager())
    dispatch_callback: Callable | None = None
    slot_timeout_sec: int = 2400
    memory_max: str = "8G"
    cpu_quota: str = "80%"

    def dispatch(
        self,
        items: list[SlotItem],
        backend: str = "claude-code",
        model: str | None = None,
        script_path: str | None = None,
    ) -> tuple[int, int]:
        """Dispatch items via transient systemd units.

        Iterates through fast lane first, then slow lane. For each item:
        - derives a slot key and unit name
        - skips if the slot is already busy
        - checks slot availability (cap + burst)
        - launches via ``dispatch_callback`` or default systemd-run

        Returns:
            (launched_count, deferred_count)
        """
        fast_items, slow_items = partition_items(items)

        launched = 0
        deferred = 0
        running = self.slot_manager.running_slots
        running_fast = self.slot_manager.running_lane_slots("fast")
        cap = self.slot_manager.slot_cap
        burst = self.slot_manager.fast_burst_allowance

        def _slot_available(lane: str) -> bool:
            """Check local slot availability (using updated counters)."""
            if running < cap:
                return True
            if lane == "fast" and running_fast < burst:
                return True
            return False

        for lane, lane_items in [("fast", fast_items), ("slow", slow_items)]:
            for item in lane_items:
                slot_key = derive_slot_key(item.repo, item.number, item.types)
                unit_name = _derive_unit_name(slot_key, lane)
                legacy_name = _derive_legacy_unit_name(slot_key)

                # Dedupe: skip if slot is already busy for this key
                if self.slot_manager._is_busy(unit_name) or self.slot_manager._is_busy(
                    legacy_name
                ):
                    deferred += 1
                    continue

                # Check slot availability (using local counters for incremental accuracy)
                if not _slot_available(lane):
                    deferred += 1
                    continue

                # Launch
                success = self._launch_unit(
                    unit_name=unit_name,
                    legacy_name=legacy_name,
                    slot_key=slot_key,
                    lane=lane,
                    item=item,
                    backend=backend,
                    model=model,
                    script_path=script_path,
                )

                if success:
                    launched += 1
                    running += 1
                    if lane == "fast":
                        running_fast += 1
                else:
                    deferred += 1

        return launched, deferred

    def _launch_unit(
        self,
        unit_name: str,
        legacy_name: str,
        slot_key: str,
        lane: str,
        item: SlotItem,
        backend: str,
        model: str | None = None,
        script_path: str | None = None,
    ) -> bool:
        """Launch a transient systemd unit for a single dispatch slot.

        If ``dispatch_callback`` is set, delegates to it. Otherwise,
        builds and executes a ``systemd-run --user`` command.
        """
        if self.dispatch_callback is not None:
            return bool(
                self.dispatch_callback(
                    slot_unit=unit_name,
                    slot_key=slot_key,
                    lane=lane,
                    item=item,
                    backend=backend,
                    model=model,
                    script_path=script_path,
                )
            )

        # Default: systemd-run
        if not script_path:
            logger.error("No script_path provided for slot dispatch")
            return False

        import subprocess
        import tempfile

        # Write item to temp file for detached unit
        slot_file = Path(tempfile.mktemp(suffix=".jsonl", prefix="pm-slot-"))
        slot_file.write_text(
            json.dumps(
                {
                    "repo": item.repo,
                    "number": item.number,
                    "types": item.types,
                    "title": item.title,
                }
            )
            + "\n"
        )

        cmd = [
            "systemd-run",
            "--user",
            "--collect",
            "--no-block",
            f"--unit={unit_name}",
            f"--description=Project monitoring slot {slot_key}",
            f"--property=TimeoutSec={self.slot_timeout_sec}",
            f"--property=MemoryMax={self.memory_max}",
            f"--property=CPUQuota={self.cpu_quota}",
            "--setenv=PM_DETACHED=1",
            "--setenv=PM_GROUPED_WORK=1",
            f"--setenv=PM_LANE={lane}",
            f"--setenv=PM_SLOT_KEY={slot_key}",
            f"--setenv=PM_DISPATCH_ID={unit_name}",
            f"--setenv=PM_WORK_FILE={slot_file}",
            f"--setenv=BOB_BACKEND={backend}",
        ]
        if model:
            cmd.append(f"--setenv=BOB_SELECTED_MODEL={model}")
        cmd.extend(["--", "bash", script_path])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True

            # Check for stale-unit error and retry once
            if "already loaded or has a fragment file" in (result.stderr or ""):
                logger.info(
                    "Stale unit %s blocking launch — resetting and retrying", unit_name
                )
                self._reset_stale_unit(unit_name)
                self._reset_stale_unit(legacy_name)
                result2 = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                return result2.returncode == 0

            return False
        except (subprocess.TimeoutExpired, OSError):
            logger.exception("Failed to launch unit %s", unit_name)
            return False

    @staticmethod
    def _reset_stale_unit(unit_name: str) -> bool:
        """Stop and reset-failed a stale transient unit."""
        import subprocess

        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed", unit_name],
                capture_output=True,
                timeout=5,
            )
            return True
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Failed to reset stale unit %s", unit_name)
            return False


def _derive_unit_name(slot_key: str, lane: str, prefix: str = "bob-pm") -> str:
    """Convert slot key to safe systemd unit name."""
    safe = slot_key.translate(str.maketrans("/#:", "---"))
    return f"{prefix}-{lane}-slot-{safe}"


def _derive_legacy_unit_name(slot_key: str, prefix: str = "bob-pm") -> str:
    """Generate the legacy (pre-lane) unit name for backward compat."""
    safe = slot_key.translate(str.maketrans("/#:", "---"))
    return f"{prefix}-slot-{safe}"


# --- SlotManager ---


@dataclass
class SlotManager:
    """Manages concurrent slot capacity and fast-lane burst allowances.

    In production this wraps ``systemctl`` calls; in tests it uses a
    provided ``count_running`` callback.
    """

    """Manages concurrent slot capacity and fast-lane burst allowances.

    In production this wraps ``systemctl`` calls; in tests it uses a
    provided ``count_running`` callback.
    """

    def __init__(
        self,
        slot_cap: int = DEFAULT_SLOT_CAP,
        fast_burst_allowance: int = DEFAULT_FAST_BURST_ALLOWANCE,
        count_running: Callable | None = None,
        count_running_lane: Callable | None = None,
        is_busy: Callable | None = None,
    ) -> None:
        self.slot_cap = slot_cap
        self.fast_burst_allowance = fast_burst_allowance

        # Injected callbacks for testability. Default to systemctl probes.
        self._count_running = count_running or _default_count_running
        self._count_running_lane = count_running_lane or (
            lambda lane: _default_count_running_lane(lane)
        )
        self._is_busy = is_busy or _default_slot_is_busy

    @property
    def running_slots(self) -> int:
        """Number of currently active slot units."""
        return self._count_running()

    def running_lane_slots(self, lane: str) -> int:
        """Number of currently active slot units for a specific lane."""
        return self._count_running_lane(lane)

    def should_allow_fast_burst(self) -> bool:
        """Check if a fast-lane item can borrow a burst slot above the cap.

        Matches bash ``should_allow_fast_burst()`` logic.
        """
        running = self.running_slots
        if running < self.slot_cap:
            return True
        running_fast = self.running_lane_slots("fast")
        return running_fast < self.fast_burst_allowance

    def slot_is_available(self, lane: str) -> bool:
        """Check if a slot is available for a lane item.

        Returns True if:
          - Running slots are below cap, OR
          - It's a fast-lane item and burst is allowed.
        """
        running = self.running_slots
        if running < self.slot_cap:
            return True
        if lane == "fast":
            return self.should_allow_fast_burst()
        return False


def _default_count_running() -> int:
    """Count running slot units via systemctl."""
    import subprocess

    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "bob-pm-*-slot-*",
            "--all",
            "--no-legend",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    count = 0
    for line in result.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit and _default_slot_is_busy(unit):
            count += 1
    return count


def _default_count_running_lane(lane: str) -> int:
    """Count running slot units for a specific lane."""
    import subprocess

    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            f"bob-pm-{lane}-slot-*",
            "--all",
            "--no-legend",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    count = 0
    for line in result.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit and _default_slot_is_busy(unit):
            count += 1
    return count


def _default_slot_is_busy(unit: str) -> bool:
    """Check if a systemd unit is in a busy state."""
    import subprocess

    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = result.stdout.strip()
    return state in {"active", "activating", "reloading", "deactivating"}


# ---------------------------------------------------------------------------
# Orchestration: dispatch_grouped_items
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Summary of one dispatch_grouped_items() cycle."""

    launched: int = 0
    skipped_active: int = 0
    skipped_cap: int = 0
    failed: int = 0
    fallback_items: list[SlotItem] = field(default_factory=list)


def _sanitize_unit_name(key: str) -> str:
    """Translate a slot key into a systemd-safe unit name fragment."""
    return key.translate(str.maketrans("/#:", "---")).replace(" ", "-")


def dispatch_grouped_items(
    items: list[SlotItem],
    unit_prefix: str = "bob-pm",
    slot_cap: int = DEFAULT_SLOT_CAP,
    fast_burst_allowance: int = DEFAULT_FAST_BURST_ALLOWANCE,
    ledger: DispatchLedger | None = None,
) -> DispatchResult:
    """Orchestrate slot dispatch for a list of grouped work items.

    This is the pure-Python equivalent of the bash ``dispatch_items()``
    function.  Instead of launching systemd transient units, it simulates
    the whole dispatch cycle in-process by checking a mock ``SlotManager``
    that reads from an in-memory registry.  Callers can use the result to
    decide which items should be dispatched externally, fall back to inline
    processing, or just log the plan.

    Parameters
    ----------
    items
        Grouped ``SlotItem`` instances to dispatch.
    unit_prefix
        Prefix for generated unit names.
    slot_cap
        Maximum concurrent dispatch slots.
    fast_burst_allowance
        Extra slots above *slot_cap* reserved for fast-lane items when no
        fast worker is active.
    ledger
        Optional ``DispatchLedger`` for telemetry recording.

    Returns
    -------
    DispatchResult
        Summary of what was launched, skipped, or set aside as fallback.
    """
    # In-memory slot tracker for this dispatch cycle.
    _active: dict[str, str] = {}  # slot_key -> unit_name

    def _is_busy(key: str) -> bool:
        return key in _active

    def _count_running() -> int:
        return len(_active)

    def _count_running_lane(lane: str) -> int:
        # The in-memory tracker doesn't track lane by default, but we can
        # approximate by checking if any fast items are still active.
        # For accurate lane tracking the caller should inject real callbacks.
        return 0  # conservative: assume no fast items running

    mgr = SlotManager(
        slot_cap=slot_cap,
        fast_burst_allowance=fast_burst_allowance,
        count_running=_count_running,
        count_running_lane=_count_running_lane,
        is_busy=_is_busy,
    )

    fast, slow = partition_items(items)
    result = DispatchResult()

    for lane, lane_items in [("fast", fast), ("slow", slow)]:
        for item in lane_items:
            key = derive_slot_key(item.repo, item.number, item.types)

            if _is_busy(key):
                result.skipped_active += 1
                if ledger:
                    ledger.append(
                        LedgerEntry.now(
                            phase="skipped_active",
                            lane=lane,
                            dispatch_id=key,
                            unit_name=f"{unit_prefix}-{_sanitize_unit_name(key)}",
                            item_refs=[key],
                            note=f"slot_already_active key={key}",
                        )
                    )
                continue

            if not mgr.slot_is_available(lane):
                result.skipped_cap += 1
                result.fallback_items.append(item)
                if ledger:
                    ledger.append(
                        LedgerEntry.now(
                            phase="skipped_cap",
                            lane=lane,
                            dispatch_id=key,
                            unit_name=f"{unit_prefix}-{_sanitize_unit_name(key)}",
                            item_refs=[key],
                            running_units=_count_running(),
                            cap=slot_cap,
                            note=f"global_slot_cap_reached key={key}",
                        )
                    )
                continue

            # Slot available — register and record
            _active[key] = f"{unit_prefix}-{_sanitize_unit_name(key)}"
            result.launched += 1
            if ledger:
                ledger.append(
                    LedgerEntry.now(
                        phase="launched",
                        lane=lane,
                        dispatch_id=key,
                        unit_name=_active[key],
                        item_refs=[key],
                        running_units=_count_running(),
                        cap=slot_cap,
                        note=f"transient_launch key={key}",
                    )
                )

    return result
