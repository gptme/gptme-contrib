"""Tests for gptme_subscription.cli."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from gptme_subscription.cli import _cmd_evaluate
from gptme_subscription.manager import Decision, SubscriptionManager


class FakeManager:
    def __init__(
        self,
        *,
        primary: str = "bob",
        switch_ok: bool = True,
        blocked: bool = False,
    ) -> None:
        self.config = SimpleNamespace(
            primary=primary,
            probe_primary_cooldown=1800,
            rate_limit_file=Path("/tmp/nonexistent-rate-limit-flag"),
        )
        self._switch_ok = switch_ok
        self._blocked = blocked
        self.detect_external_switch_calls = 0
        self.check_usage_calls: list[bool] = []
        self.switch_calls: list[tuple[str, str]] = []
        self.saved_decision: dict[str, object] | None = None
        self.cleared_rebalance = 0
        self.recorded_reset: tuple[str, float, dict[str, object]] | None = None
        self.last_switch_deferred = False

    def detect_external_switch(self) -> None:
        self.detect_external_switch_calls += 1

    def get_active_subscription(self) -> str:
        return "bob"

    def check_usage(self, no_cache: bool = False) -> dict[str, object]:
        self.check_usage_calls.append(no_cache)
        if not no_cache:
            return {
                "seven_day": {"utilization": 0.91},
                "five_hour": {"utilization": 0.10},
                "seven_day_sonnet": {"utilization": 0.20},
            }
        return {
            "seven_day": {"utilization": 0.10, "resets_in_seconds": 3600},
            "five_hour": {"utilization": 0.05},
            "seven_day_sonnet": {"utilization": 0.05},
        }

    def load_rebalance_state(self) -> None:
        return None

    def evaluate(
        self, usage: dict[str, object], active: str, *, rebalance_state=None
    ) -> Decision:
        return Decision(
            active=active,
            action="switch",
            target="alice",
            reason="rebalance to fresher slot",
            mode="forward-routing",
        )

    def seconds_since_last_primary_departure(self) -> None:
        return None

    def switch_to(self, sub: str, reason: str) -> bool:
        self.switch_calls.append((sub, reason))
        return self._switch_ok

    def save_rebalance_state(self, decision: dict[str, object]) -> None:
        self.saved_decision = decision

    def clear_rebalance_state(self) -> None:
        self.cleared_rebalance += 1

    def is_subscription_blocked(
        self, usage: dict[str, object], *, config
    ) -> tuple[bool, str]:
        if self._blocked:
            return True, "still blocked"
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
    sm = FakeManager(switch_ok=False)
    args = argparse.Namespace(json=True, execute=True, dry_run=False)

    rc = _cmd_evaluate(args, cast(SubscriptionManager, sm))

    assert rc == 1
    assert sm.check_usage_calls == [False]
    assert sm.switch_calls == [("alice", "rebalance to fresher slot")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is False
    assert payload["reason"] == "switch failed"
