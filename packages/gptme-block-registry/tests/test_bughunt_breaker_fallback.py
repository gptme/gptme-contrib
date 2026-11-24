"""Regression tests from the 2026-11-24 verified bug hunt (breaker + fallback)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from gptme_block_registry.fallback import (
    FallbackArm,
    FallbackConfig,
    decide_fallback,
    load_fallback_config,
)
from gptme_block_registry.slot_circuit_breaker import (
    AUTH_DEATH,
    NEUTRAL,
    PRODUCTIVE,
    decide_respawn,
)

ARM = FallbackArm(
    backend="gptme",
    model="openrouter/x",
    scope="lights-on",
    label="fallback:auth-death",
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    c = [100.0]
    monkeypatch.setattr(time, "time", lambda: c[0])
    return c


# R4-1 -----------------------------------------------------------------------
def test_r4_1_lost_half_open_probe_suppresses_slot_forever(
    tmp_path: Path, clock: list[float]
) -> None:
    """A granted probe whose outcome never arrives (worker killed, item skipped
    after the grant, or the probe item SUCCEEDED and is therefore never
    re-dispatched with a PRODUCTIVE verdict) leaves the slot HALF_OPEN with no
    expiry: every later spawn is suppressed and, because decide_fallback skips
    HALF_OPEN, no fallback runs either."""
    sf = tmp_path / "cb.json"
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    clock[0] = 401.0
    assert decide_respawn(state_file=sf, slot="bob", verdict=NEUTRAL) == (
        False,
        "HALF_OPEN",
    )  # probe granted; its outcome is never reported

    clock[0] = 100.0 + 1000 * 300.0  # ~3.5 days / 1000 cooldowns later
    suppress, state = decide_respawn(state_file=sf, slot="bob", verdict=NEUTRAL)
    fallback = decide_fallback(
        suppress=suppress, breaker_state=state, config=FallbackConfig(ARM)
    )
    # Either a fresh probe must be allowed or the fallback arm must run.
    assert (not suppress) or fallback is not None, (suppress, state, fallback)


# R4-2 -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "opened_at_json",
    ['"not-a-float"', "NaN", "Infinity", None],
    ids=["garbage", "nan", "inf", "missing"],
)
def test_r4_2_open_slot_with_unusable_opened_at_is_suppressed_forever(
    tmp_path: Path, clock: list[float], opened_at_json: str | None
) -> None:
    """_load_breaker keeps state=OPEN but drops/accepts an unusable opened_at;
    is_open() then can never compute an elapsed cooldown, so the slot is
    suppressed permanently — contradicting 'a corrupt state file must never
    brick the spawn loop'."""
    sf = tmp_path / "cb.json"
    field = "" if opened_at_json is None else f', "opened_at": {opened_at_json}'
    sf.write_text('{"bob": {"state": "OPEN", "failure_count": 3' + field + "}}")
    clock[0] = 10_000_000_000.0  # year 2286 — any cooldown long elapsed
    suppress, state = decide_respawn(state_file=sf, slot="bob", verdict=NEUTRAL)
    assert suppress is False, (suppress, state)


# R4-3 -----------------------------------------------------------------------
def test_r4_3_future_opened_at_after_clock_step_back_blocks_far_past_cooldown(
    tmp_path: Path, clock: list[float]
) -> None:
    """The breaker opened while the wall clock was a day fast; NTP then
    corrects it. The 300 s cooldown silently becomes ~24 h because a future
    opened_at is trusted verbatim (verdict_started_at in the future is
    rejected, opened_at is not even clamped)."""
    sf = tmp_path / "cb.json"
    clock[0] = 1_000_000.0 + 86_400.0  # clock is one day fast
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    clock[0] = 1_000_000.0 + 3600.0  # clock corrected; 1 h of real time passed
    suppress, state = decide_respawn(state_file=sf, slot="bob", verdict=NEUTRAL)
    assert suppress is False, (suppress, state, json.loads(sf.read_text()))


# R4-4 -----------------------------------------------------------------------
def test_r4_4_stale_pre_probe_deaths_reopen_breaker_right_after_recovery(
    tmp_path: Path, clock: list[float]
) -> None:
    """After a timestamped probe closes the breaker, record_success() wipes
    probe_started_at, so queued retries whose attempts started BEFORE the probe
    grant (the storm victims that were suppressed while OPEN) are counted as
    fresh failures and re-open a just-verified-healthy credential. The
    verdict_started_at contract says 'A verdict from before the probe grant is
    check-only.'"""
    sf = tmp_path / "cb.json"
    for started in (90.0, 91.0, 92.0):
        decide_respawn(
            state_file=sf, slot="bob", verdict=AUTH_DEATH, verdict_started_at=started
        )
    clock[0] = 401.0
    assert decide_respawn(
        state_file=sf, slot="bob", verdict=AUTH_DEATH, verdict_started_at=93.0
    ) == (False, "HALF_OPEN")  # stale retry becomes the probe
    clock[0] = 450.0
    assert decide_respawn(
        state_file=sf, slot="bob", verdict=PRODUCTIVE, verdict_started_at=401.5
    ) == (False, "CLOSED")  # probe succeeded: credential healthy again

    # Remaining storm victims (attempts started at t≈94-96, long before the
    # probe grant at t=401) are now dispatched with their stale verdicts.
    results = [
        decide_respawn(
            state_file=sf, slot="bob", verdict=AUTH_DEATH, verdict_started_at=s
        )
        for s in (94.0, 95.0, 96.0)
    ]
    assert results[-1] == (False, "CLOSED"), results


# R4-5 -----------------------------------------------------------------------
def test_r4_5_empty_on_auth_death_table_means_no_fallback(tmp_path: Path) -> None:
    """Docstring: 'Missing or empty [on_auth_death] section returns a config
    with on_auth_death=None'. An empty table raises ValueError instead."""
    p = tmp_path / "fallback.toml"
    p.write_text("[on_auth_death]\n")
    assert load_fallback_config(p) == FallbackConfig(on_auth_death=None)
