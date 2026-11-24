"""Regression tests from the 2026-11-24 verified bug hunt (OpenRouter 402 cap, block paths)."""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_block_registry import (
    arm_block_path,
    backend_block_path,
    block_deadline,
    limit_window_from_text,
    openrouter_block_path,
    pool_block_path,
    write_block,
)

# ---------------------------------------------------------------------------
# R5-1: per-key 402 cap misclassified as account-wide credit exhaustion
# ---------------------------------------------------------------------------

# Verbatim OpenRouter per-key-cap 402 body (fixture REC_402_KEY in the shell
# breaker's own test suite, tests/test_openrouter_breaker.py in the brain repo).
PER_KEY_402 = (
    "Error code: 402 - {'error': {'message': 'This request requires "
    "more credits, or fewer max_tokens. You requested up to 8192 tokens, but "
    "can only afford 100. To increase, visit https://openrouter.ai/settings/keys'}}"
)


def test_r5_1_per_key_cap_402_is_not_account_credits() -> None:
    """openrouter.py claims to port the shell breaker. The shell rule
    (Erik 2026-11-12) is: a 402 naming a per-key cap ("can only afford",
    a /settings/keys link) is a per-key cap -> block that context only
    ("daily"), never the account. The Python port matches the substring
    "requires more credits" first and returns "credits"."""
    assert limit_window_from_text(PER_KEY_402) != "credits"


def test_r5_1_per_key_cap_402_deadline_is_not_4h_credit_reprobe() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 24, 6, 0, 0, tzinfo=UTC)
    window = limit_window_from_text(PER_KEY_402)
    assert window is not None
    # A key cap resets at UTC midnight; re-probing in 4h just re-hits the cap.
    assert block_deadline(window, now) == datetime(2026, 11, 25, tzinfo=UTC)


# ---------------------------------------------------------------------------
# R5-2: block paths built from backend / pool / context escape state_dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda d: backend_block_path(d, "../../escaped"),
        lambda d: pool_block_path(d, "x/../../../escaped"),
        lambda d: openrouter_block_path(d, "x/../../../escaped"),
        lambda d: arm_block_path(d, "../../escaped", "m", "quality"),
    ],
    ids=["backend", "pool", "openrouter_context", "arm_backend"],
)
def test_r5_2_block_path_stays_inside_state_dir(tmp_path: Path, build) -> None:
    state_dir = tmp_path / "ws" / "state" / "backend-quota"
    state_dir.mkdir(parents=True)
    try:
        path = build(state_dir)
    except ValueError:
        return  # rejecting the unsafe name is an acceptable fix
    assert path.resolve().is_relative_to(
        state_dir.resolve()
    ), f"{path} resolves outside {state_dir}"


def test_r5_2_write_block_lands_outside_state_dir(tmp_path: Path) -> None:
    """End-to-end: write_block via a pool name writes a file outside the state dir."""
    from datetime import UTC, datetime

    state_dir = tmp_path / "ws" / "state" / "backend-quota"
    state_dir.mkdir(parents=True)
    try:
        path = pool_block_path(state_dir, "x/../../../escaped")
    except ValueError:
        return
    write_block(path, datetime(2099, 1, 1, tzinfo=UTC))
    escaped = list((tmp_path / "ws").glob("*escaped*"))
    assert escaped == [], f"block file written outside state dir: {escaped}"
