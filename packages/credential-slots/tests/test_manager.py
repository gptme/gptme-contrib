"""Tests for credential_slots.manager.

Ported from Bob's ``tests/test_manage_subscription.py`` (ErikBjare/bob,
commit e9ea27097) with paths dependency-injected via the :class:`SlotManager`
constructor instead of monkeypatching module globals.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from credential_slots import (
    DriftInfo,
    SlotManager,
    SwitchResult,
    read_slot_expiry,
    slot_is_fresh,
)


def _ms_from_now(offset_seconds: float) -> int:
    """Return a Unix epoch in milliseconds ``offset_seconds`` from now."""
    return int((datetime.now(timezone.utc).timestamp() + offset_seconds) * 1000)


def _write_slot(path: Path, expires_at_ms: int | None) -> None:
    """Write a claudeAiOauth credential file with given ``expiresAt``.

    ``expires_at_ms=None`` produces a payload with no ``expiresAt`` key,
    exercising the unknown-expiry path.
    """
    oauth: dict[str, object] = {"accessToken": "fake-token"}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


@pytest.fixture
def mgr(tmp_path: Path) -> SlotManager:
    """A SlotManager pointed at an isolated tmp creds_dir."""
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    return SlotManager(creds_dir=creds_dir, subscriptions=["bob", "alice", "erik"])


class TestConstructor:
    def test_requires_sub_placeholder_in_template(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="\\{sub\\}"):
            SlotManager(
                creds_dir=tmp_path,
                subscriptions=["bob"],
                slot_template=".bad_template_no_placeholder",
            )

    def test_requires_non_empty_subscriptions(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            SlotManager(creds_dir=tmp_path, subscriptions=[])

    def test_custom_live_name_and_template(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "custom"
        creds_dir.mkdir()
        m = SlotManager(
            creds_dir=creds_dir,
            subscriptions=["a", "b"],
            slot_template="creds-{sub}.json",
            live_name="active.json",
        )
        assert m.slot_path("a") == creds_dir / "creds-a.json"
        assert m.live_path == creds_dir / "active.json"


class TestReadSlotExpiry:
    """Reading ``expiresAt`` from a named credential slot."""

    def test_missing_slot_returns_none(self, mgr: SlotManager) -> None:
        assert mgr.read_slot_expiry("bob") is None

    def test_valid_expiry_parses(self, mgr: SlotManager) -> None:
        future_ms = _ms_from_now(3600)
        _write_slot(mgr.slot_path("bob"), future_ms)
        result = mgr.read_slot_expiry("bob")
        assert result is not None
        # Within 1 second of the written value
        assert abs(result.timestamp() * 1000 - future_ms) < 1000

    def test_missing_expiry_key_returns_none(self, mgr: SlotManager) -> None:
        """Slot file exists but has no expiresAt — treat as unknown."""
        _write_slot(mgr.slot_path("bob"), expires_at_ms=None)
        assert mgr.read_slot_expiry("bob") is None

    def test_malformed_json_returns_none(self, mgr: SlotManager) -> None:
        mgr.slot_path("bob").write_text("{not json")
        assert mgr.read_slot_expiry("bob") is None

    def test_non_dict_payload_returns_none(self, mgr: SlotManager) -> None:
        """JSON parses but root is a list — refuse rather than crash."""
        mgr.slot_path("bob").write_text('["ok"]')
        assert mgr.read_slot_expiry("bob") is None

    def test_module_function_accepts_plain_path(self, tmp_path: Path) -> None:
        """The module-level helper works without a SlotManager."""
        p = tmp_path / "creds.json"
        _write_slot(p, _ms_from_now(60))
        assert read_slot_expiry(p) is not None


class TestSlotIsFresh:
    """Whether a named slot is safe to switch to."""

    def test_future_expiry_is_fresh(self, mgr: SlotManager) -> None:
        _write_slot(mgr.slot_path("bob"), _ms_from_now(3600))
        ok, reason = mgr.slot_is_fresh("bob")
        assert ok is True
        assert "valid" in reason.lower()

    def test_past_expiry_not_fresh(self, mgr: SlotManager) -> None:
        _write_slot(mgr.slot_path("bob"), _ms_from_now(-3600))
        ok, reason = mgr.slot_is_fresh("bob")
        assert ok is False
        assert "expired" in reason.lower()

    def test_near_expiry_within_grace_not_fresh(self, mgr: SlotManager) -> None:
        """Token within the 5-min grace window is rejected."""
        _write_slot(mgr.slot_path("bob"), _ms_from_now(60))
        ok, reason = mgr.slot_is_fresh("bob", grace_seconds=300)
        assert ok is False
        assert "expir" in reason.lower()

    def test_grace_can_be_overridden_per_call(self, mgr: SlotManager) -> None:
        """Per-call grace_seconds beats the manager default."""
        _write_slot(mgr.slot_path("bob"), _ms_from_now(60))
        # Default (300) rejects; a 10s grace accepts.
        assert mgr.slot_is_fresh("bob")[0] is False
        assert mgr.slot_is_fresh("bob", grace_seconds=10)[0] is True

    def test_missing_slot_not_fresh(self, mgr: SlotManager) -> None:
        ok, reason = mgr.slot_is_fresh("bob")
        assert ok is False
        assert (
            "missing" in reason.lower()
            or "not found" in reason.lower()
            or "unreadable" in reason.lower()
        )

    def test_missing_expiry_treated_as_unknown(self, mgr: SlotManager) -> None:
        """No expiresAt in a present file → refuse (surface unusual state)."""
        _write_slot(mgr.slot_path("bob"), expires_at_ms=None)
        ok, _ = mgr.slot_is_fresh("bob")
        assert ok is False

    def test_frozen_now_parameter(self, mgr: SlotManager) -> None:
        """Passing a fixed ``now`` makes the check deterministic."""
        _write_slot(mgr.slot_path("bob"), 2_000_000_000_000)  # far future
        frozen = datetime.fromtimestamp(1_500_000_000, tz=timezone.utc)
        ok, _ = mgr.slot_is_fresh("bob", now=frozen)
        assert ok is True

    def test_module_function_accepts_plain_path(self, tmp_path: Path) -> None:
        p = tmp_path / "x.json"
        _write_slot(p, _ms_from_now(3600))
        ok, _ = slot_is_fresh(p)
        assert ok is True


class TestGetActiveSubscription:
    def test_none_when_no_symlink(self, mgr: SlotManager) -> None:
        assert mgr.get_active_subscription() is None

    def test_reads_symlink_target(self, mgr: SlotManager) -> None:
        _write_slot(mgr.slot_path("bob"), _ms_from_now(3600))
        mgr.live_path.symlink_to(".credentials.json.bob")
        assert mgr.get_active_subscription() == "bob"

    def test_unknown_target_returns_none(self, mgr: SlotManager) -> None:
        """Symlink points outside the known subscription list."""
        (mgr.creds_dir / ".credentials.json.other").write_text("{}")
        mgr.live_path.symlink_to(".credentials.json.other")
        assert mgr.get_active_subscription() is None

    def test_regular_file_returns_none(self, mgr: SlotManager) -> None:
        """Live file is a regular file (drift state) — no active sub."""
        mgr.live_path.write_text("{}")
        assert mgr.get_active_subscription() is None

    def test_path_separator_template_resolves(self, tmp_path: Path) -> None:
        """Slot template containing a path separator resolves to the right sub.

        Regression: a name-only comparison (``live.resolve().name`` against
        ``slot_template.format(...)``) silently returned None when the
        template included any directory component, because the template
        output contained the separator while the resolved target name did
        not. The fix compares resolved paths, not string names.
        """
        creds_dir = tmp_path / "creds"
        (creds_dir / "slots").mkdir(parents=True)
        m = SlotManager(
            creds_dir=creds_dir,
            subscriptions=["bob", "alice"],
            slot_template="slots/{sub}.json",
        )
        _write_slot(m.slot_path("bob"), _ms_from_now(3600))
        m.live_path.symlink_to("slots/bob.json")
        assert m.get_active_subscription() == "bob"


class TestGetAvailableSubscriptions:
    def test_empty_when_none_exist(self, mgr: SlotManager) -> None:
        assert mgr.get_available_subscriptions() == []

    def test_only_present_slots(self, mgr: SlotManager) -> None:
        _write_slot(mgr.slot_path("bob"), _ms_from_now(3600))
        _write_slot(mgr.slot_path("alice"), _ms_from_now(3600))
        # erik missing
        assert mgr.get_available_subscriptions() == ["bob", "alice"]

    def test_does_not_check_freshness(self, mgr: SlotManager) -> None:
        """``get_available_subscriptions`` only looks at existence."""
        _write_slot(mgr.slot_path("bob"), _ms_from_now(-3600))  # expired
        assert mgr.get_available_subscriptions() == ["bob"]


class TestDetectLiveSlotDrift:
    """Live credentials file should match exactly one named slot."""

    def test_live_matches_one_slot(self, mgr: SlotManager) -> None:
        blob = b'{"claudeAiOauth":{"accessToken":"x"}}'
        mgr.live_path.write_bytes(blob)
        mgr.slot_path("bob").write_bytes(blob)
        mgr.slot_path("alice").write_bytes(b'{"other":true}')
        drift = mgr.detect_live_slot_drift()
        assert drift is not None
        assert drift["drift"] is False
        assert drift["matching_slot"] == "bob"

    def test_live_matches_no_slot(self, mgr: SlotManager) -> None:
        mgr.live_path.write_bytes(b'{"claudeAiOauth":{"accessToken":"fresh"}}')
        mgr.slot_path("bob").write_bytes(
            b'{"claudeAiOauth":{"accessToken":"stale-bob"}}'
        )
        mgr.slot_path("alice").write_bytes(
            b'{"claudeAiOauth":{"accessToken":"stale-alice"}}'
        )
        drift = mgr.detect_live_slot_drift()
        assert drift is not None
        assert drift["drift"] is True
        assert drift["matching_slot"] is None
        # All slot hashes are reported
        assert set(drift["slot_hashes"].keys()) == {"bob", "alice"}

    def test_live_missing_returns_none(self, mgr: SlotManager) -> None:
        assert mgr.detect_live_slot_drift() is None

    def test_live_is_symlink_follows(self, mgr: SlotManager) -> None:
        blob = b'{"claudeAiOauth":{"accessToken":"y"}}'
        mgr.slot_path("bob").write_bytes(blob)
        mgr.live_path.symlink_to(".credentials.json.bob")
        drift = mgr.detect_live_slot_drift()
        assert drift is not None
        assert drift["drift"] is False
        assert drift["matching_slot"] == "bob"

    def test_drift_info_shape(self, mgr: SlotManager) -> None:
        """Smoke-check that returned object matches the :class:`DriftInfo` keys."""
        mgr.live_path.write_bytes(b"irrelevant")
        drift = mgr.detect_live_slot_drift()
        assert drift is not None
        expected_keys: set[str] = set(DriftInfo.__annotations__.keys())
        assert expected_keys.issubset(drift.keys())

    def test_broken_symlink_reported_as_drift(self, mgr: SlotManager) -> None:
        """Broken live symlink must not collapse into the 'no live file' branch.

        Regression: ``Path.exists()`` returns False for broken symlinks, so
        the prior ``if not live.exists(): return None`` silently masked a
        broken symlink as "nothing to compare", which is indistinguishable
        from "no live file at all". The fix: when the live path is a
        broken symlink, report it as drift with ``live_hash=None`` so
        callers can detect and repair the broken state.
        """
        # Create a broken symlink: target doesn't exist
        mgr.live_path.symlink_to(".credentials.json.bob")
        assert mgr.live_path.is_symlink()
        assert not mgr.live_path.exists()  # broken — target missing

        # Seed a slot so slot_hashes are populated
        _write_slot(mgr.slot_path("alice"), _ms_from_now(3600))

        drift = mgr.detect_live_slot_drift()
        assert drift is not None, "broken symlink should not return None"
        assert drift["drift"] is True
        assert drift["matching_slot"] is None
        assert drift["live_hash"] is None  # unreadable
        assert "alice" in drift["slot_hashes"]


class TestSwitchTo:
    """switch_to: atomic symlink flip with safety checks."""

    def _seed(
        self,
        mgr: SlotManager,
        *,
        bob_ms: int,
        alice_ms: int,
        initial: str = "alice",
    ) -> None:
        _write_slot(mgr.slot_path("bob"), bob_ms)
        _write_slot(mgr.slot_path("alice"), alice_ms)
        mgr.live_path.symlink_to(f".credentials.json.{initial}")

    def test_fresh_target_switches(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        result = mgr.switch_to("bob", "probe")
        assert result.ok is True
        assert mgr.get_active_subscription() == "bob"

    def test_expired_target_rejected(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(-3600), alice_ms=_ms_from_now(3600))
        result = mgr.switch_to("bob", "probe")
        assert result.ok is False
        assert "expired" in result.reason.lower()
        assert mgr.get_active_subscription() == "alice"

    def test_expired_target_rejected_even_with_force(self, mgr: SlotManager) -> None:
        """force bypasses the lock guard but NOT the expiry check."""
        self._seed(mgr, bob_ms=_ms_from_now(-3600), alice_ms=_ms_from_now(3600))
        result = mgr.switch_to("bob", "manual", force=True)
        assert result.ok is False
        assert mgr.get_active_subscription() == "alice"

    def test_missing_slot_rejected(self, mgr: SlotManager) -> None:
        mgr.live_path.symlink_to(".credentials.json.alice")
        # No slot files at all
        result = mgr.switch_to("bob", "probe")
        assert result.ok is False
        assert "missing" in result.reason.lower()

    def test_lock_guard_defers_switch(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        mgr.lock_guard = lambda: ["autonomous-infrastructure"]
        result = mgr.switch_to("bob", "probe")
        assert result.ok is False
        assert result.deferred_locks == ["autonomous-infrastructure"]
        assert "deferred" in result.reason.lower()
        # Live symlink unchanged
        assert mgr.get_active_subscription() == "alice"

    def test_lock_guard_empty_list_allows_switch(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        mgr.lock_guard = lambda: []  # no active locks
        result = mgr.switch_to("bob", "probe")
        assert result.ok is True

    def test_force_bypasses_lock_guard(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        mgr.lock_guard = lambda: ["busy-session"]
        result = mgr.switch_to("bob", "manual", force=True)
        assert result.ok is True
        assert mgr.get_active_subscription() == "bob"

    def test_on_switch_callback_fires_on_success(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        events: list[tuple[str, str]] = []
        mgr.on_switch = lambda sub, reason: events.append((sub, reason))
        mgr.switch_to("bob", "probe")
        assert events == [("bob", "probe")]

    def test_on_switch_not_called_on_failure(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(-3600), alice_ms=_ms_from_now(3600))
        events: list[tuple[str, str]] = []
        mgr.on_switch = lambda sub, reason: events.append((sub, reason))
        mgr.switch_to("bob", "probe")
        assert events == []

    def test_logger_receives_defer_message(self, mgr: SlotManager) -> None:
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        lines: list[str] = []
        mgr.logger = lines.append
        mgr.lock_guard = lambda: ["lock-a"]
        mgr.switch_to("bob", "probe")
        assert any("deferred" in line.lower() for line in lines)

    def test_replaces_existing_symlink(self, mgr: SlotManager) -> None:
        """Target slot change when symlink already exists (existing setup)."""
        self._seed(mgr, bob_ms=_ms_from_now(3600), alice_ms=_ms_from_now(3600))
        # initial points at alice
        assert mgr.get_active_subscription() == "alice"
        mgr.switch_to("bob", "rebalance")
        assert mgr.get_active_subscription() == "bob"
        # And back
        mgr.switch_to("alice", "revert")
        assert mgr.get_active_subscription() == "alice"


class TestSwitchResult:
    """Shape of :class:`SwitchResult`."""

    def test_defaults(self) -> None:
        r = SwitchResult(ok=True, reason="ok")
        assert r.deferred_locks == []

    def test_equality(self) -> None:
        assert SwitchResult(ok=False, reason="x", deferred_locks=["a"]) == SwitchResult(
            ok=False, reason="x", deferred_locks=["a"]
        )
