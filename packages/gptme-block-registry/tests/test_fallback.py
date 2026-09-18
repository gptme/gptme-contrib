"""Tests for the config-driven fallback arm (auth-death → cheap OpenRouter).

Alice's narrow case: when the per-slot circuit breaker opens due to auth deaths,
decide_fallback() returns a FallbackDecision that names the cheap OpenRouter arm,
carries the "lights-on" scope, and stamps a label on the session record.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_block_registry.fallback import (
    LIGHTS_ON_CATEGORIES,
    LIGHTS_ON_SCOPE,
    FallbackArm,
    FallbackConfig,
    FallbackDecision,
    decide_fallback,
    load_fallback_config,
)
from gptme_block_registry.slot_circuit_breaker import (
    AUTH_DEATH,
    PRODUCTIVE,
    decide_respawn,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CHEAP_ARM = FallbackArm(
    backend="gptme",
    model="openrouter/deepseek/deepseek-chat-v3-0324:free",
    scope=LIGHTS_ON_SCOPE,
    label="fallback:auth-death",
    note="Cheap OpenRouter arm — Claude credential down",
)

CONFIG_WITH_FALLBACK = FallbackConfig(on_auth_death=CHEAP_ARM)
CONFIG_NO_FALLBACK = FallbackConfig()


def _trip_breaker(state_file: Path, threshold: int = 3) -> tuple[bool, str]:
    """Record enough AUTH_DEATH verdicts to trip the circuit open."""
    suppress, state = False, "CLOSED"
    for _ in range(threshold):
        suppress, state = decide_respawn(
            state_file=state_file,
            slot="test-slot",
            verdict=AUTH_DEATH,
            threshold=threshold,
            cooldown=300.0,
        )
    return suppress, state


# ---------------------------------------------------------------------------
# Unit tests for FallbackArm scope machinery
# ---------------------------------------------------------------------------


class TestFallbackArmScope:
    def test_lights_on_restricts_categories(self) -> None:
        assert CHEAP_ARM.scope == LIGHTS_ON_SCOPE
        allowed = CHEAP_ARM.allowed_categories()
        assert allowed is not None
        assert "standup" in allowed
        assert "monitoring" in allowed
        assert "escalation" in allowed

    def test_lights_on_blocks_arc_work(self) -> None:
        assert CHEAP_ARM.is_category_allowed("arc") is False
        assert CHEAP_ARM.is_category_allowed("strategic") is False

    def test_unrestricted_scope_allows_all(self) -> None:
        arm = FallbackArm(
            backend="gptme",
            model="openrouter/x/y",
            scope="unrestricted",
            label="test",
        )
        assert arm.allowed_categories() is None
        assert arm.is_category_allowed("arc") is True
        assert arm.is_category_allowed("strategic") is True

    def test_lights_on_categories_not_empty(self) -> None:
        assert len(LIGHTS_ON_CATEGORIES) >= 3

    def test_label_is_set(self) -> None:
        assert CHEAP_ARM.label == "fallback:auth-death"


# ---------------------------------------------------------------------------
# Alice's narrow case: Claude auth death → cheap OpenRouter arm
# ---------------------------------------------------------------------------


class TestDecideFallback:
    def test_no_fallback_when_not_suppressed(self) -> None:
        result = decide_fallback(
            suppress=False,
            breaker_state="CLOSED",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is None

    def test_no_fallback_when_no_config(self) -> None:
        # Circuit is open but no fallback configured → suppress silently
        result = decide_fallback(
            suppress=True,
            breaker_state="OPEN",
            config=CONFIG_NO_FALLBACK,
        )
        assert result is None

    def test_fallback_fires_on_open_circuit(self) -> None:
        """Alice's narrow case: breaker OPEN → FallbackDecision returned."""
        result = decide_fallback(
            suppress=True,
            breaker_state="OPEN",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is not None
        assert isinstance(result, FallbackDecision)
        assert result.triggered_by == "auth_death"

    def test_fallback_returns_correct_arm(self) -> None:
        result = decide_fallback(
            suppress=True,
            breaker_state="OPEN",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is not None
        assert result.arm.backend == "gptme"
        assert "openrouter" in result.arm.model
        assert result.arm.scope == LIGHTS_ON_SCOPE

    def test_fallback_carries_session_label(self) -> None:
        """The label that must be stamped on fallback session records."""
        result = decide_fallback(
            suppress=True,
            breaker_state="OPEN",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is not None
        assert result.arm.label == "fallback:auth-death"

    def test_no_fallback_on_half_open_suppression(self) -> None:
        # HALF_OPEN suppression is a probe cycle — don't layer a fallback on top.
        result = decide_fallback(
            suppress=True,
            breaker_state="HALF_OPEN",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is None

    def test_no_fallback_on_unknown_state(self) -> None:
        result = decide_fallback(
            suppress=True,
            breaker_state="UNKNOWN",
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is None


# ---------------------------------------------------------------------------
# End-to-end: decide_respawn → decide_fallback pipeline
# ---------------------------------------------------------------------------


class TestFallbackPipeline:
    def test_full_auth_death_pipeline(self, tmp_path: Path) -> None:
        """Full pipeline: repeated auth deaths trip the breaker, fallback fires."""
        state_file = tmp_path / "breaker.json"
        suppress, state = _trip_breaker(state_file, threshold=3)

        assert suppress is True
        assert state == "OPEN"

        result = decide_fallback(
            suppress=suppress,
            breaker_state=state,
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is not None
        assert result.triggered_by == "auth_death"
        assert result.arm.model == CHEAP_ARM.model

    def test_no_fallback_on_healthy_arm(self, tmp_path: Path) -> None:
        state_file = tmp_path / "breaker.json"
        suppress, state = decide_respawn(
            state_file=state_file,
            slot="test-slot",
            verdict=PRODUCTIVE,
        )
        assert suppress is False
        result = decide_fallback(
            suppress=suppress,
            breaker_state=state,
            config=CONFIG_WITH_FALLBACK,
        )
        assert result is None

    def test_no_fallback_before_threshold(self, tmp_path: Path) -> None:
        """Fallback must NOT fire before the breaker opens."""
        state_file = tmp_path / "breaker.json"
        threshold = 3
        for i in range(threshold - 1):  # One short of tripping
            suppress, state = decide_respawn(
                state_file=state_file,
                slot="test-slot",
                verdict=AUTH_DEATH,
                threshold=threshold,
            )
            result = decide_fallback(
                suppress=suppress,
                breaker_state=state,
                config=CONFIG_WITH_FALLBACK,
            )
            assert result is None, f"Fallback fired prematurely at auth-death #{i+1}"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadFallbackConfig:
    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        cfg = load_fallback_config(tmp_path / "nonexistent.toml")
        assert cfg.on_auth_death is None

    def test_load_valid_config(self, tmp_path: Path) -> None:
        toml = tmp_path / "fallback.toml"
        toml.write_text(
            "[on_auth_death]\n"
            'backend = "gptme"\n'
            'model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"\n'
            'scope   = "lights-on"\n'
            'label   = "fallback:auth-death"\n'
            'note    = "survival arm"\n',
            encoding="utf-8",
        )
        cfg = load_fallback_config(toml)
        assert cfg.on_auth_death is not None
        arm = cfg.on_auth_death
        assert arm.backend == "gptme"
        assert arm.model == "openrouter/deepseek/deepseek-chat-v3-0324:free"
        assert arm.scope == LIGHTS_ON_SCOPE
        assert arm.label == "fallback:auth-death"
        assert arm.note == "survival arm"

    def test_empty_toml_no_section_returns_empty_config(self, tmp_path: Path) -> None:
        toml = tmp_path / "fallback.toml"
        toml.write_text("[other_section]\nfoo = 1\n", encoding="utf-8")
        cfg = load_fallback_config(toml)
        assert cfg.on_auth_death is None

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "fallback.toml"
        toml.write_text(
            "[on_auth_death]\n" 'backend = "gptme"\n',
            # model, scope, label missing
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required field"):
            load_fallback_config(toml)

    def test_unknown_scope_raises(self, tmp_path: Path) -> None:
        """Typo like 'lights_on' (underscore) must fail closed, not become unrestricted."""
        toml = tmp_path / "fallback.toml"
        toml.write_text(
            "[on_auth_death]\n"
            'backend = "gptme"\n'
            'model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"\n'
            'scope   = "lights_on"\n'  # underscore typo
            'label   = "fallback:auth-death"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="not a known scope"):
            load_fallback_config(toml)

    def test_non_string_scope_raises(self, tmp_path: Path) -> None:
        """A TOML bool scope (scope = true) must fail, not become unrestricted."""
        toml = tmp_path / "fallback.toml"
        toml.write_text(
            "[on_auth_death]\n"
            'backend = "gptme"\n'
            'model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"\n'
            "scope   = true\n"  # wrong type
            'label   = "fallback:auth-death"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="non-empty string"):
            load_fallback_config(toml)

    def test_loaded_config_drives_decide_fallback(self, tmp_path: Path) -> None:
        """Round-trip: load from TOML → decide_fallback fires correctly."""
        toml = tmp_path / "fallback.toml"
        toml.write_text(
            "[on_auth_death]\n"
            'backend = "gptme"\n'
            'model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"\n'
            'scope   = "lights-on"\n'
            'label   = "fallback:auth-death"\n',
            encoding="utf-8",
        )
        cfg = load_fallback_config(toml)

        state_file = tmp_path / "breaker.json"
        suppress, state = _trip_breaker(state_file, threshold=3)

        result = decide_fallback(suppress=suppress, breaker_state=state, config=cfg)
        assert result is not None
        assert result.arm.label == "fallback:auth-death"
        assert result.arm.scope == LIGHTS_ON_SCOPE
