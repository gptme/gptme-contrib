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

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

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

# Minimum bandit observations before preferring bandit routing over static fallback
MIN_BANDIT_OBSERVATIONS = 5

logger = logging.getLogger(__name__)


def is_direct_mention(detail: str) -> bool:
    """Return True when *detail* marks a direct Erik @Bob mention handoff.

    Mirrors the bash ``item_detail_is_direct_mention()`` function in
    ``project-monitoring-dispatch.sh``.  Two upstream sources produce
    different marker shapes:

    * ``activity-gate.sh`` emits the raw GitHub notification ``reason`` as
      detail tokens joined by ``"; "``.  A bare ``"mention"`` token means a
      direct @Bob mention; ``"team_mention"`` and others do not qualify.
    * ``assigned_issue_pending_reply.py`` embeds
      ``source: direct_mention_handoff`` inside the detail string.

    Direct Erik mentions are high-signal asks — dispatching them to the
    cheap fast-lane model causes NOOPs (ErikBjare/bob#907).
    """
    if "direct_mention_handoff" in detail:
        return True
    for tok in detail.split(";"):
        if tok.strip() == "mention":
            return True
    return False


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
    detail: str = ""


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


# --- Bash-compatible full ledger entry ---
#
# The bash ``append_dispatch_ledger()`` function writes a richer schema than
# ``LedgerEntry.to_dict()``: it preserves a ``unit`` field name, plus
# ``item_count``, ``types`` (deduped sorted), and ``items`` (capped at 20)
# derived from the dispatched work file. ``build_full_ledger_entry()`` is the
# canonical Python reimplementation so the bash heredoc can be replaced with
# a thin module call without changing the on-disk JSONL schema.


def _maybe_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_full_ledger_entry(
    *,
    phase: str,
    lane: str = "mixed",
    dispatch_id: str | None = None,
    unit_name: str | None = None,
    work_file: str | Path | None = None,
    running_units: str | int | None = None,
    cap: str | int | None = None,
    note: str | None = None,
    successes: str | int | None = None,
    failures: str | int | None = None,
    duration_seconds: str | int | None = None,
    timestamp: str | None = None,
    max_items: int = 20,
) -> dict[str, Any]:
    """Build the full bash-compatible dispatch ledger entry.

    Mirrors the on-disk schema produced by the bash
    ``append_dispatch_ledger()`` function:
    ``timestamp/phase/lane/dispatch_id/unit/item_count/item_refs/types/items``
    plus optional ``running_units/cap/note/successes/failures/duration_seconds``.

    The ``items`` list is capped at *max_items* entries (default 20). Items
    are read from *work_file* (a JSONL of grouped items) when provided.
    """
    items: list[dict[str, Any]] = []
    type_set: set[str] = set()
    item_refs: list[str] = []

    if work_file:
        path = Path(work_file)
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                repo = str(item.get("repo", "") or "")
                number = item.get("number")
                item_types = item.get("types")
                if not isinstance(item_types, list) or not item_types:
                    single_type = item.get("type")
                    item_types = [single_type] if isinstance(single_type, str) else []
                normalized_types = [t for t in item_types if isinstance(t, str) and t]
                type_set.update(normalized_types)
                if number is not None:
                    item_refs.append(f"{repo}#{number}")
                items.append(
                    {
                        "repo": repo,
                        "number": number,
                        "types": normalized_types,
                        "title": item.get("title"),
                    }
                )

    return {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "lane": lane,
        "dispatch_id": dispatch_id or None,
        "unit": unit_name or None,
        "item_count": len(items),
        "item_refs": list(dict.fromkeys(item_refs)),
        "types": sorted(type_set),
        "items": items[:max_items],
        "running_units": _maybe_int(running_units),
        "cap": _maybe_int(cap),
        "note": note or None,
        "successes": _maybe_int(successes),
        "failures": _maybe_int(failures),
        "duration_seconds": _maybe_int(duration_seconds),
    }


def append_full_ledger_entry(
    ledger_path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a full ledger entry and append it to *ledger_path* (JSONL).

    Returns the entry dict that was written. ``kwargs`` are forwarded to
    :func:`build_full_ledger_entry`.
    """
    entry = build_full_ledger_entry(**kwargs)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# --- Slot key derivation ---


def _slugify_check(name: str) -> str:
    """Slugify a CI check/workflow name for use in a slot key.

    Lowercases, collapses non-alphanumerics to single dashes, trims dashes.
    Empty/whitespace-only names fall back to ``"unknown"``.
    All-punctuation inputs (non-blank but produce an empty slug) use a short
    hash so they never collide with a check literally named ``"unknown"``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug:
        return slug
    if not name.strip():
        return "unknown"
    # Non-blank name but all-punctuation: use a hash to avoid collision with
    # a check literally named "unknown".
    return "x" + hashlib.sha1(name.encode()).hexdigest()[:6]


def derive_slot_key(
    repo: str, number: int | None, types: list[str], check: str | None = None
) -> str:
    """Derive a slot key from a work item.

    Matches the bash ``derive_slot_key()`` logic:
      - master_ci_failure types → ``{repo}#master-ci:{check-slug}`` when a
        check/workflow name is known, else ``{repo}#master-ci``
      - others → ``{repo}#{number}``

    The per-check slug is what keeps two *concurrent* master-CI failures on the
    same repo (e.g. ``test-e2e`` and ``Conformance Check``) from collapsing into
    one slot — a long-running failure on check A must never mask detection or
    dispatch of a new failure on check B.
    """
    if "master_ci_failure" in types:
        if check and check.strip():
            return f"{repo}#master-ci:{_slugify_check(check)}"
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


def classify_item_work_type(types: list[str]) -> str:
    """Map SlotItem.types to a PM bandit work type string.

    Priority order is significant: ci-fix and greptile-fix take precedence over
    pr-review since a PR with a CI failure should route to the CI-fix arm, not
    the generic PR-review arm.

    Returns one of the PM_WORK_TYPES strings defined in pm_bandit.
    """
    types_set = set(types)
    if "strategy" in types_set:
        return "strategy-reply"
    if types_set & {"ci_failure", "master_ci_failure"}:
        return "ci-fix"
    if types_set & {"greptile_needs_fix", "greptile_needs_improvement"}:
        return "greptile-fix"
    if "merge_conflict" in types_set:
        return "merge-conflict"
    if "pr_update" in types_set:
        return "pr-review"
    if "assigned" in types_set:
        return "assigned-issue"
    return "notification-triage"


def _bandit_observation_count(
    bandit: Any, work_type: str, models: list[str] | None = None
) -> int:
    """Count recorded outcomes for work_type, optionally filtered to specific models.

    When *models* is provided, only counts observations for those model names.
    When None, counts across all models (legacy — prefer passing available models
    to avoid threshold crossings driven by retired model arms).
    """
    total = 0
    try:
        summary = bandit.summary().get(work_type, {})
        for model_name, model_data in summary.items():
            if models is not None and model_name not in models:
                continue
            total += int(model_data.get("selections", 0))
    except (AttributeError, TypeError):
        pass
    return total


def _resolve_model_with_bandit(
    item_types: list[str],
    lane: str,
    model: str | None,
    fast_model: str | None,
    bandit: Any | None,
    detail: str = "",
) -> str | None:
    """Resolve the dispatch model for an item.

    Direct Erik @mention items (``is_direct_mention(detail) == True``) always
    get the base *model* — never the cheap *fast_model* — regardless of lane or
    bandit state.  This mirrors the bash ``select_slot_model()`` precedence rule
    and prevents the NOOP failure from ErikBjare/bob#907.

    For all other items: when a bandit is provided and has ≥
    MIN_BANDIT_OBSERVATIONS for the inferred work type, use Thompson sampling
    to select the model.  Otherwise fall back to the static
    ``resolve_lane_model()`` split.
    """
    # Direct @mention override: never downgrade to the cheap fast-lane model.
    if is_direct_mention(detail):
        return model

    if bandit is None:
        return resolve_lane_model(lane, model, fast_model)
    work_type = classify_item_work_type(item_types)
    available = [m for m in [model, fast_model] if m]
    if not available:
        available = ["sonnet"]
    obs = _bandit_observation_count(bandit, work_type, models=available)
    if obs < MIN_BANDIT_OBSERVATIONS:
        return resolve_lane_model(lane, model, fast_model)
    return cast(str, bandit.resolve_model(work_type, available))


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


def resolve_lane_model(
    lane: str,
    base_model: str | None,
    fast_model: str | None = None,
) -> str | None:
    """Resolve the model to use for a dispatch *lane*.

    Per-lane model routing (ErikBjare/bob#860): the fast lane (notifications,
    assigned-issue triage — everything not in ``SLOW_LANE_TYPES``) is the
    low-intelligence-needed partition, so it can run on a cheaper model while
    the slow lane (PR reviews, CI diagnostics, Greptile, merge conflicts) keeps
    the capable model.

    Opt-in and default-off: with ``fast_model`` unset (``None``), every lane
    gets ``base_model`` — identical to the prior single-model behavior. The
    bash runtime mirrors this via the ``BOB_PM_FAST_LANE_MODEL`` env var.
    """
    if lane == "fast" and fast_model:
        return fast_model
    return base_model


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
                    entries.append(LedgerEntry(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
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
        fast_model: str | None = None,
        bandit: Any = None,
    ) -> tuple[int, int]:
        """Dispatch items via transient systemd units.

        Iterates through fast lane first, then slow lane. For each item:
        - derives a slot key and unit name
        - skips if the slot is already busy
        - checks slot availability (cap + burst)
        - resolves the per-lane model (see ``resolve_lane_model``)
        - launches via ``dispatch_callback`` or default systemd-run

        ``fast_model`` (falling back to the ``BOB_PM_FAST_LANE_MODEL`` env var)
        opts the fast lane onto a cheaper model; unset preserves the single
        ``model`` for every lane (ErikBjare/bob#860).

        ``bandit``, when provided, overrides the static lane-based split with
        Thompson-sampling routing per work type once ≥ MIN_BANDIT_OBSERVATIONS
        outcomes have been recorded. Falls back to ``resolve_lane_model()``
        below the threshold. Pass a :class:`~gptme_runloops.pm_bandit.PmModelBandit`
        instance.

        Returns:
            (launched_count, deferred_count)
        """
        if fast_model is None:
            fast_model = os.environ.get("BOB_PM_FAST_LANE_MODEL") or None
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
                slot_key = derive_slot_key(
                    item.repo, item.number, item.types, item.title
                )
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

                # Launch — bandit-driven model selection when observations ≥ threshold
                success = self._launch_unit(
                    unit_name=unit_name,
                    legacy_name=legacy_name,
                    slot_key=slot_key,
                    lane=lane,
                    item=item,
                    backend=backend,
                    model=_resolve_model_with_bandit(
                        item.types, lane, model, fast_model, bandit, item.detail
                    ),
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

        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".jsonl", prefix="pm-slot-", delete=False
        ) as tmp:
            slot_file = Path(tmp.name)
            tmp.write(
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


@dataclass(eq=False)
class SlotManager:
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
        self._count_running_lane = count_running_lane or _default_count_running_lane
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
    skipped_cooldown: int = 0
    failed: int = 0
    fallback_items: list[SlotItem] = field(default_factory=list)


# --- Human-review dispatch priority (§7b human > bot) ---
#
# Erik's CHANGES_REQUESTED review on gptme/gptme#3178 (2026-07-11) was emitted
# by the gate every cycle but skipped_cap twice while all slots drained a
# 3-day bot-priority backlog. The §7b "human > bot" invariant was only
# enforced in suppression logic (cooldown bypass), not in dispatch ordering or
# capacity. These tokens are emitted by activity-gate.sh into the item
# ``detail`` field (they survive build_grouped_jsonl's ``join("; ")`` the same
# way the ``mention`` reason token does) and consumed here for lane ordering
# and by the bash dispatcher for the bounded cap-overflow rule.

# A human's latest review on the PR is CHANGES_REQUESTED — blocking merge.
HUMAN_PRIORITY_CHANGES_REQUESTED = "human_changes_requested"
# The most recent comment/review actor on the PR is a human (non-bot, non-self).
HUMAN_PRIORITY_ACTIVITY = "human_activity"

_HUMAN_PRIORITY_RANKS = {
    HUMAN_PRIORITY_CHANGES_REQUESTED: 0,
    HUMAN_PRIORITY_ACTIVITY: 1,
}
DEFAULT_PRIORITY_RANK = 2

# Bounded overflow: at most this many slots above the global cap may be
# consumed by human-priority items (mirrored by the bash dispatcher's
# PM_HUMAN_OVERFLOW_ALLOWANCE, default 1).
HUMAN_PRIORITY_OVERFLOW_ALLOWANCE = 1


def item_priority_rank(detail: str | None) -> int:
    """Priority rank of an item from its ``detail`` tokens (lower = first).

    Tokens are ``"; "``-joined in the detail field (same convention as
    :func:`is_direct_mention`), so exact-token matching avoids false positives
    on free text. Returns the best (lowest) rank present:
    0 = human CHANGES_REQUESTED, 1 = human activity, 2 = default (bot/none).
    """
    rank = DEFAULT_PRIORITY_RANK
    for tok in (detail or "").split(";"):
        tok_rank = _HUMAN_PRIORITY_RANKS.get(tok.strip())
        if tok_rank is not None and tok_rank < rank:
            rank = tok_rank
    return rank


def detail_is_human_priority(detail: str | None) -> bool:
    """True when the item's detail carries a human-priority token."""
    return item_priority_rank(detail) < DEFAULT_PRIORITY_RANK


def human_priority_allows_overflow(
    detail: str | None,
    running_slots: int,
    slot_cap: int,
    overflow_allowance: int = HUMAN_PRIORITY_OVERFLOW_ALLOWANCE,
) -> bool:
    """Cap-overflow rule: may a human-priority item dispatch above the cap?

    Chosen over slot *reservation* because reservation permanently cuts bot
    throughput to ``cap - 1`` even when no human item exists and requires
    classifying already-running units by priority. Overflow is strictly
    additive and self-bounding: ``running_slots`` counts every live slot
    (including a previously granted overflow slot), so at most
    *overflow_allowance* slots ever run above the cap.
    """
    if not detail_is_human_priority(detail):
        return False
    if overflow_allowance <= 0:
        return False
    return running_slots < slot_cap + overflow_allowance


# --- LRU lane ordering (slot-starvation fairness) ---

# Same convention as the bash runtime's per-slot dispatch cooldown markers
# (``record_slot_dispatch`` in project-monitoring-dispatch.sh writes
# ``<slot_safe>.ts`` files holding an epoch timestamp).
DISPATCH_COOLDOWN_DIR_ENV = "PM_DISPATCH_COOLDOWN_DIR"
DEFAULT_DISPATCH_COOLDOWN_DIR = "/tmp/bob-pm-dispatch-cooldown"

# Per-slot dispatch cooldown (ErikBjare/bob#788). Mirrors bash
# PM_DISPATCH_COOLDOWN_SECS (default 600, 0 = disabled).
DISPATCH_COOLDOWN_SECS_ENV = "PM_DISPATCH_COOLDOWN_SECS"
DEFAULT_DISPATCH_COOLDOWN_SECS = 600


def _resolve_cooldown_dir(cooldown_dir: Path | None) -> Path:
    return cooldown_dir or Path(
        os.environ.get(DISPATCH_COOLDOWN_DIR_ENV) or DEFAULT_DISPATCH_COOLDOWN_DIR
    )


def _slot_in_cooldown(slot_safe: str, cooldown_secs: int, cooldown_dir: Path) -> bool:
    """True when *slot_safe* was dispatched within *cooldown_secs* seconds.

    Mirrors bash ``slot_dispatch_in_cooldown()`` in
    ``project-monitoring-dispatch.sh``.  Returns False when cooldown_secs ≤ 0,
    when no marker exists, or when the marker is unreadable.
    """
    if cooldown_secs <= 0:
        return False
    marker = cooldown_dir / f"{slot_safe}.ts"
    try:
        last = int(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    return (int(time.time()) - last) < cooldown_secs


def _write_slot_dispatch_marker(slot_safe: str, cooldown_dir: Path) -> None:
    """Record that *slot_safe* was just dispatched (mirrors ``record_slot_dispatch``).

    Best-effort: on ``OSError`` a warning is logged and the marker is skipped,
    meaning no cooldown suppression for that slot.  This matches the bash
    reference, which also does not roll back if the file write fails.
    """
    try:
        cooldown_dir.mkdir(parents=True, exist_ok=True)
        (cooldown_dir / f"{slot_safe}.ts").write_text(str(int(time.time())))
    except OSError as e:
        logger.warning("Failed to write dispatch marker for %s: %s", slot_safe, e)


def _item_types(data: dict[str, Any]) -> list[str]:
    item_types = data.get("types") or []
    if not isinstance(item_types, list) or not item_types:
        t = data.get("type")
        item_types = [t] if isinstance(t, str) else []
    return item_types


def _item_slot_safe(data: dict[str, Any]) -> str:
    """Slot-key of a grouped-item dict, sanitized like the bash slot_safe."""
    key = derive_slot_key(
        str(data.get("repo") or ""),
        data.get("number"),
        _item_types(data),
        data.get("title"),
    )
    return _sanitize_unit_name(key)


def _last_dispatch_epoch(slot_safe: str, cooldown_dir: Path) -> int:
    """Epoch of the slot's last dispatch, 0 when never dispatched / unreadable."""
    try:
        return int((cooldown_dir / f"{slot_safe}.ts").read_text().strip())
    except (OSError, ValueError):
        return 0


def order_lane_lru(
    lane_items: list[dict[str, Any]], cooldown_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Order a lane's items least-recently-dispatched first (starvation fairness).

    The dispatch loop consumes each lane file top-to-bottom under a global slot
    cap, so without ordering, whatever the gate pipeline emitted first wins the
    remaining cap every cycle and long-blocked items (e.g. sub-floor-Greptile
    PRs) starve indefinitely (observed 2026-07-09: ``skipped_cap`` 13-31/hour
    while the same early items re-dispatched).

    Ordering: human-priority items first (§7b human > bot — see
    :func:`item_priority_rank`; CHANGES_REQUESTED ahead of plain human
    activity), then within each priority class never-dispatched items first
    (no cooldown marker → epoch 0), then ascending by last-dispatch epoch.
    The sort is stable, so ties — including a fresh cooldown dir after
    reboot — preserve the original gate order, i.e. with no human-priority
    items this degrades gracefully to pure LRU / the previous behavior.
    Freshness is preserved: a brand-new mention has no marker and therefore
    sorts first anyway.
    """
    if cooldown_dir is None:
        cooldown_dir = Path(
            os.environ.get(DISPATCH_COOLDOWN_DIR_ENV) or DEFAULT_DISPATCH_COOLDOWN_DIR
        )
    have_cooldown = cooldown_dir.is_dir()

    def _key(data: dict[str, Any]) -> tuple[int, int]:
        detail = data.get("detail")
        epoch = (
            _last_dispatch_epoch(_item_slot_safe(data), cooldown_dir)
            if have_cooldown
            else 0
        )
        return (item_priority_rank(detail if isinstance(detail, str) else None), epoch)

    return sorted(lane_items, key=_key)


def _partition_jsonl_io(
    fast_path: Path,
    slow_path: Path,
    items: list[SlotItem] | None = None,
) -> None:
    """Partition grouped items JSONL from stdin or *items* into fast/slow lane files.

    Reads JSONL from *stdin* when *items* is ``None`` (the primary bash-bridge path).
    Each JSONL line is parsed, lane-classified, ordered least-recently-dispatched
    first within its lane (see ``order_lane_lru``), and written to the
    corresponding file. Silently skips blank lines.
    Ensures both output files exist on return even when no items are written.
    """
    fast_path.parent.mkdir(parents=True, exist_ok=True)
    slow_path.parent.mkdir(parents=True, exist_ok=True)
    fast_path.touch()
    slow_path.touch()
    if items is None:
        # Read JSONL from stdin (bash bridge path)
        lanes: dict[str, list[dict[str, Any]]] = {"fast": [], "slow": []}
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("skipping unparseable JSONL line: %.80s", raw)
                continue
            lanes[classify_lane(_item_types(data))].append(data)
        for lane, target_path in (("fast", fast_path), ("slow", slow_path)):
            with target_path.open("a", encoding="utf-8") as fh:
                for data in order_lane_lru(lanes[lane]):
                    fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        return

    # Direct SlotItem path (Python-to-Python)
    fast, slow = partition_items(items)
    for f_item in fast:
        with fast_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(f_item.__dict__, ensure_ascii=False) + "\n")
    for s_item in slow:
        with slow_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(s_item.__dict__, ensure_ascii=False) + "\n")


def _sanitize_unit_name(key: str) -> str:
    """Translate a slot key into a systemd-safe unit name fragment."""
    return key.translate(str.maketrans("/#:", "---")).replace(" ", "-")


def dispatch_grouped_items(
    items: list[SlotItem],
    unit_prefix: str = "bob-pm",
    slot_cap: int = DEFAULT_SLOT_CAP,
    fast_burst_allowance: int = DEFAULT_FAST_BURST_ALLOWANCE,
    ledger: DispatchLedger | None = None,
    cooldown_secs: int | None = None,
    cooldown_dir: Path | None = None,
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
    cooldown_secs
        Per-slot dispatch cooldown in seconds (mirrors bash
        ``PM_DISPATCH_COOLDOWN_SECS``, default 600, 0 = disabled).  When
        ``None``, reads ``PM_DISPATCH_COOLDOWN_SECS`` from the environment,
        falling back to ``DEFAULT_DISPATCH_COOLDOWN_SECS``.
    cooldown_dir
        Directory for cooldown timestamp markers.  Defaults to
        ``PM_DISPATCH_COOLDOWN_DIR`` env var or ``DEFAULT_DISPATCH_COOLDOWN_DIR``.

    Returns
    -------
    DispatchResult
        Summary of what was launched, skipped, or set aside as fallback.

    Notes
    -----
    Cooldown markers are written **optimistically** — before the caller performs
    the actual external launch (e.g. ``systemd-run``).  If the external launch
    fails after this function returns, the marker remains, suppressing re-dispatch
    for up to *cooldown_secs*.  This matches the bash reference
    (``project-monitoring-dispatch.sh``) where ``record_slot_dispatch`` is called
    immediately before ``systemd-run`` with no rollback on failure.
    """
    if cooldown_secs is None:
        _env_val = os.environ.get(DISPATCH_COOLDOWN_SECS_ENV, "")
        try:
            cooldown_secs = (
                int(_env_val) if _env_val else DEFAULT_DISPATCH_COOLDOWN_SECS
            )
        except ValueError:
            cooldown_secs = DEFAULT_DISPATCH_COOLDOWN_SECS
    _cooldown_dir = _resolve_cooldown_dir(cooldown_dir)
    # In-memory slot tracker for this dispatch cycle.
    _active: dict[str, str] = {}  # slot_key -> unit_name
    _active_lanes: dict[str, str] = {}  # slot_key -> lane

    def _is_busy(key: str) -> bool:
        return key in _active

    def _count_running() -> int:
        return len(_active)

    def _count_running_lane(lane: str) -> int:
        return sum(1 for active_lane in _active_lanes.values() if active_lane == lane)

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
            key = derive_slot_key(item.repo, item.number, item.types, item.title)
            slot_safe = _sanitize_unit_name(key)

            if _is_busy(key):
                result.skipped_active += 1
                if ledger:
                    ledger.append(
                        LedgerEntry.now(
                            phase="skipped_active",
                            lane=lane,
                            dispatch_id=key,
                            unit_name=f"{unit_prefix}-{slot_safe}",
                            item_refs=[key],
                            note=f"slot_already_active key={key}",
                        )
                    )
                continue

            # Cooldown backstop (ErikBjare/bob#788): skip re-dispatch within
            # the cooldown window even if the prior worker already finished.
            # Two bypasses mirror the bash dispatcher:
            #   (a) merge_conflict items — gate doesn't state-track conflicts,
            #       so they nag every run; a pr_update cooldown on the same slot
            #       must never suppress a freshly-detected conflict.
            #   (b) comment-driven items (pr_update/assigned_issue) whose detail
            #       carries a human-priority token — human follow-ups always get
            #       a session, same as merge_conflict (pm-cooldown task).
            if _slot_in_cooldown(slot_safe, cooldown_secs, _cooldown_dir):
                _has_conflict = "merge_conflict" in item.types
                _is_comment_driven = bool(
                    set(item.types) & {"pr_update", "assigned_issue"}
                )
                _bypass = _has_conflict or (
                    _is_comment_driven and detail_is_human_priority(item.detail)
                )
                if not _bypass:
                    result.skipped_cooldown += 1
                    if ledger:
                        ledger.append(
                            LedgerEntry.now(
                                phase="skipped_cooldown",
                                lane=lane,
                                dispatch_id=key,
                                unit_name=f"{unit_prefix}-{slot_safe}",
                                item_refs=[key],
                                note=f"dispatch_cooldown key={key}",
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
                            unit_name=f"{unit_prefix}-{slot_safe}",
                            item_refs=[key],
                            running_units=_count_running(),
                            cap=slot_cap,
                            note=f"global_slot_cap_reached key={key}",
                        )
                    )
                continue

            # Slot available — register and record
            _active[key] = f"{unit_prefix}-{slot_safe}"
            _active_lanes[key] = lane
            _write_slot_dispatch_marker(slot_safe, _cooldown_dir)
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


def _append_ledger_main(argv: list[str]) -> int:
    """CLI handler for the ``append-ledger`` subcommand."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="pm_dispatch append-ledger",
        description="Append a dispatch telemetry entry to a JSONL ledger.",
    )
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--lane", default="mixed")
    parser.add_argument("--dispatch-id", default="")
    parser.add_argument("--unit", default="")
    parser.add_argument("--work-file", default="")
    parser.add_argument("--running-units", default="")
    parser.add_argument("--cap", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--successes", default="")
    parser.add_argument("--failures", default="")
    parser.add_argument("--duration-seconds", default="")
    args = parser.parse_args(argv)

    append_full_ledger_entry(
        args.ledger_path,
        phase=args.phase,
        lane=args.lane,
        dispatch_id=args.dispatch_id or None,
        unit_name=args.unit or None,
        work_file=args.work_file or None,
        running_units=args.running_units,
        cap=args.cap,
        note=args.note or None,
        successes=args.successes,
        failures=args.failures,
        duration_seconds=args.duration_seconds,
    )
    return 0


def _record_bandit_outcome_main(argv: list[str]) -> int:
    """CLI handler for the ``record-bandit-outcome`` subcommand.

    Loads the PmModelBandit and records one dispatch outcome. Designed for
    bash post-session hooks to call after a slot session completes.

    Example::

        python3 -m gptme_runloops.pm_dispatch record-bandit-outcome \\
            --work-type ci-fix --model haiku --outcome productive
    """
    import argparse

    from gptme_runloops.pm_bandit import PmModelBandit

    parser = argparse.ArgumentParser(
        prog="pm_dispatch record-bandit-outcome",
        description="Record a PM dispatch outcome into the bandit state.",
    )
    parser.add_argument(
        "--work-type",
        required=True,
        help="PM work type (e.g. ci-fix, greptile-fix, pr-review)",
    )
    parser.add_argument(
        "--model", required=True, help="Model that handled the dispatch (e.g. haiku)"
    )
    parser.add_argument(
        "--outcome",
        required=True,
        help="Session outcome: 'productive', 'failed', or a float 0-1",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Override bandit state directory (default: state/pm-dispatch)",
    )
    args = parser.parse_args(argv)

    from gptme_runloops.pm_bandit import PM_WORK_TYPES

    if args.work_type not in PM_WORK_TYPES:
        parser.error(
            f"--work-type {args.work_type!r} is not a known PM work type; "
            f"valid values: {sorted(PM_WORK_TYPES)}"
        )

    _VALID_OUTCOMES = {"productive", "failed"}
    outcome_val: str | float
    try:
        outcome_val = float(args.outcome)
    except ValueError:
        if args.outcome not in _VALID_OUTCOMES:
            parser.error(
                f"--outcome must be 'productive', 'failed', or a float 0-1; "
                f"got {args.outcome!r}"
            )
        outcome_val = args.outcome

    bandit = PmModelBandit(state_dir=args.state_dir)
    bandit.record_outcome(args.work_type, args.model, outcome_val)
    logger.info(
        "Recorded outcome: work_type=%s model=%s outcome=%s",
        args.work_type,
        args.model,
        args.outcome,
    )
    return 0


def _records_aggregate_main(argv: list[str]) -> int:
    """CLI handler for the ``records-aggregate`` subcommand.

    Reads all ``*.json`` files from a directory, parses each, and emits a
    single JSON array on stdout. Files with invalid JSON or encoding errors are
    skipped silently; OS-level errors propagate.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="pm_dispatch records-aggregate",
        description="Aggregate per-item monitoring records into a single JSON array.",
    )
    parser.add_argument(
        "--records-dir",
        required=True,
        help="Directory containing per-item JSON record files",
    )
    args = parser.parse_args(argv)

    records: list[Any] = []
    records_dir = Path(args.records_dir)
    if records_dir.is_dir():
        for path in sorted(records_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    print(json.dumps(records, ensure_ascii=False))
    return 0


def main() -> None:
    """CLI entry point.

    Subcommands:

      partition <fast_out> <slow_out>      (legacy default — also no subcmd)
        Read grouped JSONL items from stdin, write fast/slow lane files.

      append-ledger --ledger-path P --phase X ...
        Append a dispatch telemetry entry to a JSONL ledger.

      records-aggregate --records-dir DIR
        Aggregate per-item JSON records into a single JSON array on stdout.

      record-bandit-outcome --work-type W --model M --outcome O
        Record a dispatch outcome into the PmModelBandit state.
    """
    import sys as _sys

    argv = _sys.argv[1:]

    # Subcommand dispatch
    if argv and argv[0] == "append-ledger":
        _sys.exit(_append_ledger_main(argv[1:]))
    if argv and argv[0] == "records-aggregate":
        _sys.exit(_records_aggregate_main(argv[1:]))
    if argv and argv[0] == "record-bandit-outcome":
        _sys.exit(_record_bandit_outcome_main(argv[1:]))
    if argv and argv[0] == "partition":
        argv = argv[1:]

    if len(argv) < 2:
        print(
            "Usage:\n"
            "  python3 -m gptme_runloops.pm_dispatch [partition] <fast_out> <slow_out>\n"
            "  python3 -m gptme_runloops.pm_dispatch append-ledger --ledger-path P --phase X [...]\n"
            "  python3 -m gptme_runloops.pm_dispatch records-aggregate --records-dir DIR",
            file=_sys.stderr,
        )
        _sys.exit(1)

    fast_path = Path(argv[0])
    slow_path = Path(argv[1])
    _partition_jsonl_io(fast_path, slow_path)
    fast_count = (
        len(fast_path.read_text().splitlines()) if fast_path.stat().st_size else 0
    )
    slow_count = (
        len(slow_path.read_text().splitlines()) if slow_path.stat().st_size else 0
    )
    print(
        f"Dispatched: {fast_count} fast + {slow_count} slow = {fast_count + slow_count} total",
        file=_sys.stderr,
    )


if __name__ == "__main__":
    main()
