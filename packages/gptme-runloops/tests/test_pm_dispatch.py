"""Tests for pm_dispatch module (dispatch primitives)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gptme_runloops.pm_dispatch import (
    DEFAULT_FAST_BURST_ALLOWANCE,
    DEFAULT_SLOT_CAP,
    SLOW_LANE_TYPES,
    DispatchLedger,
    LedgerEntry,
    SlotItem,
    SlotManager,
    classify_lane,
    derive_slot_key,
    partition_items,
)

# --- Fixtures ---


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "dispatch.jsonl"


@pytest.fixture
def ledger(ledger_path: Path) -> DispatchLedger:
    return DispatchLedger(ledger_path)


def make_item(
    repo: str = "owner/repo",
    number: int = 42,
    types: list[str] | None = None,
    title: str = "Test item",
) -> SlotItem:
    return SlotItem(
        repo=repo,
        number=number,
        types=types or ["pr_update"],
        title=title,
        url=f"https://github.com/{repo}/pull/{number}",
    )


# --- Derive slot key ---


class TestDeriveSlotKey:
    def test_standard_item(self):
        assert derive_slot_key("o/r", 99, ["pr_update"]) == "o/r#99"

    def test_master_ci_failure(self):
        assert derive_slot_key("o/r", 99, ["master_ci_failure"]) == "o/r#master-ci"

    def test_master_ci_with_other_types(self):
        assert (
            derive_slot_key("o/r", 99, ["master_ci_failure", "ci_failure"])
            == "o/r#master-ci"
        )

    def test_no_number(self):
        assert derive_slot_key("o/r", None, ["pr_update"]) == "o/r#unknown"


# --- Lane classification ---


class TestClassifyLane:
    def test_pr_update_is_slow(self):
        assert classify_lane(["pr_update"]) == "slow"

    def test_ci_failure_is_slow(self):
        assert classify_lane(["ci_failure"]) == "slow"

    def test_master_ci_failure_is_slow(self):
        assert classify_lane(["master_ci_failure"]) == "slow"

    def test_merge_conflict_is_slow(self):
        assert classify_lane(["merge_conflict"]) == "slow"

    def test_greptile_is_slow(self):
        assert classify_lane(["greptile_needs_fix"]) == "slow"
        assert classify_lane(["greptile_needs_improvement"]) == "slow"

    def test_notification_is_fast(self):
        assert classify_lane(["notification"]) == "fast"

    def test_assigned_issue_is_fast(self):
        assert classify_lane(["assigned_issue"]) == "fast"

    def test_mixed_types_with_slow_is_slow(self):
        assert classify_lane(["notification", "ci_failure"]) == "slow"

    def test_all_slow_types_covered(self):
        """Verify all expected slow-lane types are in the set."""
        expected = {
            "pr_update",
            "ci_failure",
            "master_ci_failure",
            "merge_conflict",
            "greptile_needs_fix",
            "greptile_needs_improvement",
        }
        assert SLOW_LANE_TYPES == expected


# --- Partition items ---


class TestPartitionItems:
    def test_empty(self):
        assert partition_items([]) == ([], [])

    def test_all_fast(self):
        items = [
            make_item(types=["notification"]),
            make_item(types=["assigned_issue"]),
        ]
        fast, slow = partition_items(items)
        assert len(fast) == 2
        assert len(slow) == 0

    def test_all_slow(self):
        items = [
            make_item(types=["pr_update"]),
            make_item(types=["ci_failure"]),
        ]
        fast, slow = partition_items(items)
        assert len(fast) == 0
        assert len(slow) == 2

    def test_mixed(self):
        items = [
            make_item(types=["notification"]),
            make_item(types=["pr_update"]),
            make_item(types=["assigned_issue"]),
            make_item(types=["ci_failure"]),
        ]
        fast, slow = partition_items(items)
        assert len(fast) == 2
        assert len(slow) == 2


# --- DispatchLedger ---


class TestDispatchLedger:
    def test_append_and_read(self, ledger: DispatchLedger, ledger_path: Path):
        entry = LedgerEntry.now(
            phase="launch",
            lane="fast",
            dispatch_id="test-1",
            unit_name="bob-pm-fast-slot-test-1",
            item_refs=["owner/repo#42"],
            running_units=1,
            cap=3,
        )
        ledger.append(entry)
        assert ledger_path.exists()
        lines = ledger_path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["phase"] == "launch"
        assert data["lane"] == "fast"
        assert data["dispatch_id"] == "test-1"
        assert data["running_units"] == 1
        assert data["cap"] == 3

    def test_read_empty(self, ledger: DispatchLedger):
        assert ledger.read() == []

    def test_read_non_existent(self, ledger_path: Path):
        ledger = DispatchLedger(ledger_path / "nonexistent" / "file.jsonl")
        assert ledger.read() == []

    def test_multiple_entries(self, ledger: DispatchLedger):
        e1 = LedgerEntry.now(
            phase="launch", lane="fast", dispatch_id="a", unit_name="u1"
        )
        e2 = LedgerEntry.now(
            phase="complete", lane="slow", dispatch_id="b", unit_name="u2"
        )
        ledger.append(e1)
        ledger.append(e2)
        entries = ledger.read()
        assert len(entries) == 2
        assert entries[0].dispatch_id == "a"
        assert entries[1].dispatch_id == "b"

    def test_clear(self, ledger: DispatchLedger):
        e1 = LedgerEntry.now(
            phase="launch", lane="fast", dispatch_id="a", unit_name="u1"
        )
        ledger.append(e1)
        assert ledger.path.exists()
        ledger.clear()
        assert not ledger.path.exists()
        assert ledger.read() == []

    def test_ledger_entry_to_dict_omits_none(self):
        entry = LedgerEntry(
            timestamp="2026-01-01T00:00:00Z",
            phase="launch",
            lane="fast",
            dispatch_id="test",
            unit_name="u1",
            item_refs=[],
        )
        d = entry.to_dict()
        assert "running_units" not in d
        assert "note" not in d


# --- SlotManager ---


class TestSlotManager:
    def test_defaults(self):
        sm = SlotManager()
        assert sm.slot_cap == DEFAULT_SLOT_CAP
        assert sm.fast_burst_allowance == DEFAULT_FAST_BURST_ALLOWANCE

    def test_slot_cap_custom(self):
        sm = SlotManager(slot_cap=5)
        assert sm.slot_cap == 5

    def test_running_slots_uses_callback(self):
        sm = SlotManager(count_running=lambda: 3)
        assert sm.running_slots == 3

    def test_slot_available_below_cap(self):
        sm = SlotManager(slot_cap=4, count_running=lambda: 2)
        assert sm.slot_is_available("slow") is True
        assert sm.slot_is_available("fast") is True

    def test_slot_available_at_cap(self):
        sm = SlotManager(slot_cap=3, count_running=lambda: 3)
        assert sm.slot_is_available("slow") is False

    def test_slot_available_fast_burst_at_cap(self):
        sm = SlotManager(
            slot_cap=3,
            fast_burst_allowance=1,
            count_running=lambda: 3,
            count_running_lane=lambda lane: 0,
        )
        # Fast lane should get burst allowance
        assert sm.slot_is_available("fast") is True

    def test_slot_available_fast_burst_exhausted(self):
        sm = SlotManager(
            slot_cap=3,
            fast_burst_allowance=1,
            count_running=lambda: 3,
            count_running_lane=lambda lane: 1 if lane == "fast" else 3,
        )
        # Fast burst exhausted (1 fast running == 1 allowance)
        assert sm.slot_is_available("fast") is False

    def test_should_allow_fast_burst_below_cap(self):
        sm = SlotManager(slot_cap=3, count_running=lambda: 2)
        assert sm.should_allow_fast_burst() is True

    def test_should_allow_fast_burst_at_cap_with_room(self):
        sm = SlotManager(
            slot_cap=3,
            fast_burst_allowance=1,
            count_running=lambda: 3,
            count_running_lane=lambda lane: 0,
        )
        assert sm.should_allow_fast_burst() is True

    def test_should_allow_fast_burst_at_cap_exhausted(self):
        sm = SlotManager(
            slot_cap=3,
            fast_burst_allowance=1,
            count_running=lambda: 3,
            count_running_lane=lambda lane: 1 if lane == "fast" else 3,
        )
        assert sm.should_allow_fast_burst() is False

    def test_running_lane_slots_uses_callback(self):
        sm = SlotManager(count_running_lane=lambda lane: 2 if lane == "fast" else 5)
        assert sm.running_lane_slots("fast") == 2
        assert sm.running_lane_slots("slow") == 5
