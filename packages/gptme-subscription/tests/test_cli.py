"""Tests for gptme_subscription.cli."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from credential_slots import slot_is_fresh
from gptme_subscription.cli import (
    _cmd_evaluate,
    _cmd_switch,
    _execute_switch_decision,
)
from gptme_subscription.manager import Decision, SubscriptionManager


class FakeManager:
    def __init__(
        self,
        *,
        primary: str = "bob",
        active: str = "bob",
        switch_results: list[tuple[bool, bool]] | None = None,
        blocked: bool = False,
        initial_usage: dict[str, object] | None = None,
        post_switch_usage: dict[str, object] | None = None,
        decision: Decision | None = None,
        blocked_reason: str = "still blocked",
        slot_fresh_result: tuple[bool, str] = (True, "ok"),
    ) -> None:
        self.config = SimpleNamespace(
            primary=primary,
            subscriptions=[primary, "alice", "erik"],
            probe_primary_cooldown=1800,
            rate_limit_file=Path("/tmp/nonexistent-rate-limit-flag"),
            slot_path=lambda name: Path(f"/tmp/fake-slot-{name}.json"),
            usage_script=None,
        )
        self._active = active
        self._switch_results = list(switch_results or [(True, False)])
        self._slot_fresh_result = slot_fresh_result
        self._blocked = blocked
        self._initial_usage = (
            initial_usage
            if initial_usage is not None
            else {
                "seven_day": {"utilization": 0.91},
                "five_hour": {"utilization": 0.10},
                "seven_day_sonnet": {"utilization": 0.20},
            }
        )
        self._post_switch_usage = (
            post_switch_usage
            if post_switch_usage is not None
            else {
                "seven_day": {"utilization": 0.10, "resets_in_seconds": 3600},
                "five_hour": {"utilization": 0.05},
                "seven_day_sonnet": {"utilization": 0.05},
            }
        )
        self._decision = decision or Decision(
            active=active,
            action="switch",
            target="alice",
            reason="rebalance to fresher slot",
            mode="forward-routing",
        )
        self._blocked_reason = blocked_reason
        self.detect_external_switch_calls = 0
        self.check_usage_calls: list[bool] = []
        self.switch_calls: list[tuple[str, str]] = []
        self.switch_probe_ok_flags: list[bool] = []
        self.slot_is_fresh_calls: list[str] = []
        self.saved_decision: dict[str, object] | None = None
        self.cleared_rebalance = 0
        self.manual_hold_calls: list[str] = []
        self.recorded_reset: tuple[str, float, dict[str, object]] | None = None
        self.last_switch_deferred = False

    def detect_external_switch(self) -> None:
        self.detect_external_switch_calls += 1

    def get_active_subscription(self) -> str:
        return self._active

    def check_usage(
        self, no_cache: bool = False, stale_cache: Path | None = None
    ) -> dict[str, object]:
        self.check_usage_calls.append(no_cache)
        return self._post_switch_usage if no_cache else self._initial_usage

    def load_rebalance_state(self) -> None:
        return None

    def evaluate(
        self, usage: dict[str, object], active: str, *, rebalance_state=None
    ) -> Decision:
        return self._decision

    def seconds_since_last_primary_departure(self) -> None:
        return None

    def switch_to(
        self, sub: str, reason: str, force: bool = False, probe_ok: bool = False
    ) -> bool:
        self.switch_calls.append((sub, reason))
        self.switch_probe_ok_flags.append(probe_ok)
        ok, deferred = self._switch_results.pop(0)
        self.last_switch_deferred = deferred
        return ok

    def slot_is_fresh(self, sub: str, grace_seconds: int = 300) -> tuple[bool, str]:
        self.slot_is_fresh_calls.append(sub)
        return self._slot_fresh_result

    def save_rebalance_state(self, decision: dict[str, object]) -> None:
        self.saved_decision = decision

    def clear_rebalance_state(self) -> None:
        self.cleared_rebalance += 1

    def record_manual_switch_hold(self, target: str) -> None:
        self.manual_hold_calls.append(target)

    def is_subscription_blocked(
        self, usage: dict[str, object], *, config
    ) -> tuple[bool, str]:
        if self._blocked:
            return True, self._blocked_reason
        return False, "healthy"

    def record_sub_reset_time(
        self, sub: str, resets_in_seconds: float, usage: dict[str, object]
    ) -> None:
        self.recorded_reset = (sub, resets_in_seconds, usage)


def test_cmd_evaluate_json_execute_runs_post_switch_verification(
    capsys,
) -> None:
    sm = FakeManager()
    args = argparse.Namespace(json=True, execute=True, dry_run=False)

    rc = _cmd_evaluate(args, cast(SubscriptionManager, sm))

    assert rc == 0
    assert sm.detect_external_switch_calls == 1
    assert sm.check_usage_calls == [False, True]
    assert sm.switch_calls == [("alice", "rebalance to fresher slot")]
    assert sm.saved_decision is not None
    assert sm.recorded_reset is not None
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is True
    assert payload["verified"] is True
    assert payload["verification_reason"] == "healthy"
    assert payload["post_switch_usage"]["seven_day"]["utilization"] == 0.10


def test_cmd_evaluate_json_execute_returns_failure_on_switch_error(capsys) -> None:
    sm = FakeManager(switch_results=[(False, False)])
    args = argparse.Namespace(json=True, execute=True, dry_run=False)

    rc = _cmd_evaluate(args, cast(SubscriptionManager, sm))

    assert rc == 1
    assert sm.check_usage_calls == [False]
    assert sm.switch_calls == [("alice", "rebalance to fresher slot")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is False
    assert payload["reason"] == "switch refused"


def test_execute_switch_decision_does_not_claim_revert_when_usage_check_revert_fails() -> (
    None
):
    sm = FakeManager(
        active="alice",
        switch_results=[(True, False), (False, False)],
        post_switch_usage={},
        decision=Decision(
            active="alice",
            action="switch",
            target="bob",
            reason="probe bob",
        ),
    )

    rc, payload = _execute_switch_decision(
        cast(SubscriptionManager, sm),
        sm._decision,
        "alice",
        emit_text=False,
    )

    assert rc == 0
    assert payload["executed"] is True
    assert payload["verified"] is False
    assert "reverted_to" not in payload
    assert payload["revert_failed"] is True
    assert payload["revert_reason"] == "revert failed"
    assert sm.switch_calls == [
        ("bob", "probe bob"),
        ("alice", "auto-revert: usage check failed"),
    ]


def test_execute_switch_decision_reports_deferred_primary_revert() -> None:
    sm = FakeManager(
        active="alice",
        switch_results=[(True, False), (False, True)],
        blocked=True,
        decision=Decision(
            active="alice",
            action="switch",
            target="bob",
            reason="probe bob",
        ),
        blocked_reason="still blocked",
    )

    rc, payload = _execute_switch_decision(
        cast(SubscriptionManager, sm),
        sm._decision,
        "alice",
        emit_text=False,
    )

    assert rc == 0
    assert payload["executed"] is True
    assert payload["verified"] is True
    assert payload["verification_reason"] == "still blocked"
    assert "reverted_to" not in payload
    assert payload["revert_failed"] is True
    assert payload["revert_deferred"] is True
    assert payload["revert_reason"] == "revert deferred by active locks"
    assert sm.switch_calls == [
        ("bob", "probe bob"),
        ("alice", "auto-revert: still blocked"),
    ]


def test_execute_switch_decision_does_not_claim_revert_for_blocked_forward_routing() -> (
    None
):
    sm = FakeManager(
        active="bob",
        switch_results=[(True, False), (False, False)],
        blocked=True,
        decision=Decision(
            active="bob",
            action="switch",
            target="alice",
            reason="rebalance to fresher slot",
            mode="forward-routing",
        ),
        blocked_reason="already blocked",
    )

    rc, payload = _execute_switch_decision(
        cast(SubscriptionManager, sm),
        sm._decision,
        "bob",
        emit_text=False,
    )

    assert rc == 0
    assert payload["executed"] is True
    assert payload["verified"] is True
    assert payload["verification_reason"] == "already blocked"
    assert "reverted_to" not in payload
    assert payload["revert_failed"] is True
    assert payload["revert_reason"] == "revert failed"
    assert sm.switch_calls == [
        ("alice", "rebalance to fresher slot"),
        ("bob", "auto-revert routing: alice blocked"),
    ]


def test_execute_switch_decision_emits_alert_on_refused_switch(capsys) -> None:
    """A refused (not deferred) switch should print [ALERT] to stderr."""
    sm = FakeManager(
        active="bob",
        switch_results=[(False, False)],  # refused, not deferred
        decision=Decision(
            active="bob",
            action="switch",
            target="alice",
            reason="weekly exhausted",
        ),
    )

    rc, payload = _execute_switch_decision(
        cast(SubscriptionManager, sm),
        sm._decision,
        "bob",
        emit_text=True,
    )

    assert rc == 1
    assert payload["reason"] == "switch refused"
    captured = capsys.readouterr()
    assert "[ALERT]" in captured.err
    assert "alice" in captured.err
    assert "reauth" in captured.err.lower()


def test_execute_switch_decision_no_alert_on_deferred_switch(capsys) -> None:
    """A deferred switch (active locks) must NOT emit [ALERT]."""
    sm = FakeManager(
        active="bob",
        switch_results=[(False, True)],  # deferred by locks
        decision=Decision(
            active="bob",
            action="switch",
            target="alice",
            reason="weekly exhausted",
        ),
    )

    rc, payload = _execute_switch_decision(
        cast(SubscriptionManager, sm),
        sm._decision,
        "bob",
        emit_text=True,
    )

    assert rc == 0
    assert payload.get("deferred") is True
    captured = capsys.readouterr()
    assert "[ALERT]" not in captured.err


def test_cmd_switch_execute_writes_manual_hold_not_clear() -> None:
    # A successful manual --switch must record a protective hold (so a concurrent
    # automated --execute can't immediately route away), not clear hold state.
    sm = FakeManager(active="bob")
    args = argparse.Namespace(switch="alice", execute=True, dry_run=False)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 0
    assert sm.switch_calls == [("alice", "manual switch via --switch alice")]
    assert sm.manual_hold_calls == ["alice"]
    assert sm.cleared_rebalance == 0


def test_cmd_switch_unknown_slot_returns_error(capsys) -> None:
    sm = FakeManager(active="bob")
    args = argparse.Namespace(switch="nobody", execute=True, dry_run=False)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 1
    assert sm.switch_calls == []
    assert sm.manual_hold_calls == []


def test_cmd_switch_dry_run_does_not_switch_or_hold(capsys) -> None:
    sm = FakeManager(active="bob")
    args = argparse.Namespace(switch="alice", execute=False, dry_run=True)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 0
    assert sm.switch_calls == []
    assert sm.manual_hold_calls == []


def _real_fresh_reason(
    tmp_path: Path, *, expires_in: float | None, write: bool = True
) -> tuple[bool, str]:
    """Produce a genuine ``slot_is_fresh`` result for a synthetic slot file.

    Using the real producer (rather than a hand-typed string) keeps these CLI
    tests honest: if ``slot_is_fresh``'s reason wording changes, the classifier
    it feeds is exercised against the new wording here too.

    ``write=False`` leaves the file absent (missing-slot reason);
    ``expires_in=None`` writes a payload with no ``expiresAt`` (unreadable).
    """
    path = tmp_path / ".credentials.json.alice"
    if write:
        oauth: dict[str, object] = {"accessToken": "fake"}
        if expires_in is not None:
            oauth["expiresAt"] = int(
                (datetime.now(timezone.utc).timestamp() + expires_in) * 1000
            )
        path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return slot_is_fresh(path)


@pytest.mark.parametrize(
    "expires_in,label",
    [
        (-3600, "already expired"),
        # Regression for the grace-window branch: its reason reads
        # "expire*s* within grace", so a naive `"expired" in reason` check
        # skipped the probe and failed the operator's --switch outright.
        (60, "within the 5-min grace window"),
    ],
)
def test_cmd_switch_stale_token_probes_and_retries_with_probe_ok(
    expires_in: float, label: str, tmp_path: Path, monkeypatch
) -> None:
    """A stale-but-refreshable slot is probed, then re-switched with probe_ok=True."""
    fresh, reason = _real_fresh_reason(tmp_path, expires_in=expires_in)
    assert fresh is False, f"fixture should be stale ({label})"

    sm = FakeManager(
        active="bob",
        switch_results=[(False, False), (True, False)],
        slot_fresh_result=(fresh, reason),
    )
    args = argparse.Namespace(switch="alice", execute=True, dry_run=False)

    probe_mock = MagicMock(return_value=(None, True, "probe ok"))
    monkeypatch.setattr("gptme_subscription.cli.probe_credential", probe_mock)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 0
    assert sm.slot_is_fresh_calls == ["alice"]
    probe_mock.assert_called_once()
    assert probe_mock.call_args.args[0] == sm.config.slot_path("alice")
    assert sm.switch_calls == [
        ("alice", "manual switch via --switch alice"),
        ("alice", "manual switch via --switch alice (probe_ok)"),
    ]
    assert sm.switch_probe_ok_flags == [False, True]
    assert sm.manual_hold_calls == ["alice"]


def test_cmd_switch_stale_token_probe_failure_does_not_retry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A failed probe means the refresh token is dead: no retry, rc 1, no hold."""
    fresh, reason = _real_fresh_reason(tmp_path, expires_in=-3600)
    sm = FakeManager(
        active="bob",
        switch_results=[(False, False)],
        slot_fresh_result=(fresh, reason),
    )
    args = argparse.Namespace(switch="alice", execute=True, dry_run=False)

    monkeypatch.setattr(
        "gptme_subscription.cli.probe_credential",
        MagicMock(return_value=(None, False, "probe failed: 401")),
    )

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 1
    assert len(sm.switch_calls) == 1
    assert sm.switch_probe_ok_flags == [False]
    assert sm.manual_hold_calls == []
    assert "probe failed: 401" in capsys.readouterr().err


@pytest.mark.parametrize(
    "expires_in,write",
    [
        (None, True),  # present but no expiresAt → "unreadable ..."
        (0, False),  # absent file → "slot missing: ..."
    ],
)
def test_cmd_switch_unprobeable_failure_is_not_probed(
    expires_in: float | None, write: bool, tmp_path: Path, monkeypatch
) -> None:
    """Missing/unreadable slots can't be fixed by a probe — fail hard, don't probe."""
    fresh, reason = _real_fresh_reason(tmp_path, expires_in=expires_in, write=write)
    assert fresh is False

    probe_mock = MagicMock(return_value=(None, True, "ok"))
    monkeypatch.setattr("gptme_subscription.cli.probe_credential", probe_mock)

    sm = FakeManager(
        active="bob",
        switch_results=[(False, False)],
        slot_fresh_result=(fresh, reason),
    )
    args = argparse.Namespace(switch="alice", execute=True, dry_run=False)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 1
    probe_mock.assert_not_called()
    assert len(sm.switch_calls) == 1
    assert sm.manual_hold_calls == []


def test_cmd_switch_fresh_slot_failure_is_not_probed(
    tmp_path: Path, monkeypatch
) -> None:
    """If the slot is fresh, the switch failed for some other reason — no probe."""
    fresh, reason = _real_fresh_reason(tmp_path, expires_in=3600)
    assert fresh is True

    probe_mock = MagicMock(return_value=(None, True, "ok"))
    monkeypatch.setattr("gptme_subscription.cli.probe_credential", probe_mock)

    sm = FakeManager(
        active="bob",
        switch_results=[(False, True)],  # deferred by locks
        slot_fresh_result=(fresh, reason),
    )
    args = argparse.Namespace(switch="alice", execute=True, dry_run=False)

    rc = _cmd_switch(args, cast(SubscriptionManager, sm))

    assert rc == 1
    probe_mock.assert_not_called()
    assert sm.manual_hold_calls == []
