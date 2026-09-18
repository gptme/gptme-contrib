"""Registry evaluation tests: ordering, fail-open, and policy-free composition."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gptme_block_registry import BlockRegistry, write_block

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=6)
PAST = NOW - timedelta(hours=6)


def test_missing_files_are_clear(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    verdict = registry.check(registry.arm_checks("gptme", "glm-5.2"), now=NOW)
    assert verdict.blocked is False
    assert verdict.warnings == []


def test_future_deadline_blocks(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    write_block(registry.arm_checks("gptme", "glm-5.2")[0].path, FUTURE)
    verdict = registry.check(registry.arm_checks("gptme", "glm-5.2"), now=NOW)
    assert verdict.blocked is True
    assert verdict.reason == "crash_loop"
    assert verdict.until == FUTURE


def test_expired_deadline_is_clear(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    write_block(registry.arm_checks("gptme", "glm-5.2")[0].path, PAST)
    verdict = registry.check(registry.arm_checks("gptme", "glm-5.2"), now=NOW)
    assert verdict.blocked is False


def test_ordering_first_active_wins(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    checks = registry.arm_checks("gptme", "glm-5.2")
    # crash_loop blocked AND quality blocked → crash_loop reported first
    write_block(checks[0].path, FUTURE)
    write_block(checks[2].path, FUTURE + timedelta(hours=1))
    verdict = registry.check(checks, now=NOW)
    assert verdict.reason == "crash_loop"


def test_garbled_timestamp_fails_open_with_warning(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    checks = registry.arm_checks("gptme", "glm-5.2")
    checks[0].path.parent.mkdir(parents=True, exist_ok=True)
    checks[0].path.write_text("corrupted-not-a-date\n")
    verdict = registry.check(checks, now=NOW)
    assert verdict.blocked is False
    assert len(verdict.warnings) == 1
    assert "fail-open" in verdict.warnings[0]


def test_backend_and_pool_checks_compose(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    write_block(registry.backend_check("copilot-cli").path, FUTURE)
    checks = registry.arm_checks("copilot-cli", "sonnet") + [
        registry.backend_check("copilot-cli")
    ]
    verdict = registry.check(checks, now=NOW)
    assert verdict.blocked is True
    assert verdict.reason == "backend_blocked"

    pool_registry = BlockRegistry(tmp_path)
    write_block(pool_registry.pool_check("chatgpt-sub").path, FUTURE)
    pool_verdict = pool_registry.check(
        [pool_registry.pool_check("chatgpt-sub")], now=NOW
    )
    assert pool_verdict.reason == "pool_blocked"


def test_openrouter_scoped_and_shared_contexts_are_distinct(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    write_block(registry.openrouter_check("deepseek").path, FUTURE)
    # scoped context blocked
    assert registry.check([registry.openrouter_check("deepseek")], now=NOW).blocked
    # shared chain unaffected
    assert not registry.check([registry.openrouter_check()], now=NOW).blocked


def test_naive_now_is_treated_as_utc(tmp_path: Path) -> None:
    """A caller-supplied naive `now` must not crash the comparison against the
    tz-aware parsed deadline (P2 regression)."""
    registry = BlockRegistry(tmp_path)
    checks = registry.arm_checks("gptme", "glm-5.2")
    write_block(checks[0].path, FUTURE)
    naive_now = NOW.replace(tzinfo=None)
    verdict = registry.check(checks, now=naive_now)
    assert verdict.blocked is True
    assert verdict.reason == "crash_loop"


def test_verdict_as_dict_shape(tmp_path: Path) -> None:
    registry = BlockRegistry(tmp_path)
    write_block(registry.arm_checks("gptme", "m")[0].path, FUTURE)
    payload = registry.check(registry.arm_checks("gptme", "m"), now=NOW).as_dict()
    assert payload["blocked"] is True
    assert payload["reason"] == "crash_loop"
    assert "until" in payload and "file" in payload
    assert "reason" not in registry.check([], now=NOW).as_dict()
