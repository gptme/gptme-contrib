"""Tests for attention_router plugin."""

from unittest.mock import patch

import pytest


@pytest.fixture
def temp_state_file(tmp_path):
    """Create temporary state file."""
    state_file = tmp_path / ".gptme" / "attention_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with patch("gptme_attention_router.tools.attention_router.STATE_FILE", state_file):
        yield state_file


@pytest.fixture
def reset_state():
    """Reset global state before each test."""
    from gptme_attention_router.tools import attention_router

    attention_router._state = None
    yield
    attention_router._state = None


def test_register_file(temp_state_file, reset_state):
    """Test registering a file for tracking."""
    from gptme_attention_router.tools.attention_router import register_file, get_score

    result = register_file(
        "test/file.md", keywords=["test", "example"], pinned=True, initial_score=0.6
    )

    assert "Registered" in result
    assert get_score("test/file.md") == 0.6


def test_unregister_file(temp_state_file, reset_state):
    """Test unregistering a file."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        unregister_file,
        get_score,
    )

    register_file("test/file.md")
    result = unregister_file("test/file.md")

    assert "Removed" in result
    assert get_score("test/file.md") is None


def test_process_turn_decay(temp_state_file, reset_state):
    """Test that scores decay on process_turn."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        process_turn,
        get_score,
    )

    register_file("test/file.md", initial_score=1.0)
    process_turn("unrelated message")

    # Score should decay by default rate (0.75)
    assert get_score("test/file.md") == pytest.approx(0.75, rel=0.01)


def test_process_turn_activation(temp_state_file, reset_state):
    """Test keyword activation on process_turn."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        process_turn,
        get_score,
    )

    register_file("test/file.md", keywords=["hello"], initial_score=0.3)
    result = process_turn("hello world")

    assert "test/file.md" in result["activated"]
    assert get_score("test/file.md") == 1.0


def test_get_tiers(temp_state_file, reset_state):
    """Test tier assignment."""
    from gptme_attention_router.tools.attention_router import register_file, get_tiers

    register_file("hot/file.md", initial_score=0.9)
    register_file("warm/file.md", initial_score=0.5)
    register_file("cold/file.md", initial_score=0.1)

    tiers = get_tiers()

    assert any(f["path"] == "hot/file.md" for f in tiers["HOT"])
    assert any(f["path"] == "warm/file.md" for f in tiers["WARM"])
    assert any(f["path"] == "cold/file.md" for f in tiers["COLD"])


def test_pinned_file_minimum(temp_state_file, reset_state):
    """Test that pinned files never fall below WARM."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        process_turn,
        get_score,
    )

    register_file("pinned/file.md", pinned=True, initial_score=0.3)

    # Process many turns to decay
    for _ in range(20):
        process_turn("unrelated")

    # Pinned file should stay at WARM threshold
    assert get_score("pinned/file.md") >= 0.25


def test_coactivation(temp_state_file, reset_state):
    """Test co-activation boosting."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        process_turn,
        get_score,
    )

    register_file(
        "primary/file.md", keywords=["trigger"], coactivate_with=["related/file.md"]
    )
    register_file("related/file.md", initial_score=0.3)

    process_turn("trigger word")

    # Primary should be HOT (1.0)
    assert get_score("primary/file.md") == 1.0
    # Related should be boosted (0.3 * decay + 0.35 boost)
    assert get_score("related/file.md") > 0.5


def test_get_context_recommendation(temp_state_file, reset_state):
    """Test context recommendation."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        get_context_recommendation,
    )

    register_file("hot1/file.md", initial_score=0.9)
    register_file("hot2/file.md", initial_score=0.85)
    register_file("warm/file.md", initial_score=0.5)
    register_file("cold/file.md", initial_score=0.1)

    rec = get_context_recommendation(max_hot=2, max_warm=1)

    assert len(rec["include_full"]) == 2
    assert len(rec["include_header"]) == 1
    assert rec["excluded_count"] == 1


def test_set_score(temp_state_file, reset_state):
    """Test manually setting score."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        set_score,
        get_score,
    )

    register_file("test/file.md", initial_score=0.5)
    result = set_score("test/file.md", 0.9)

    assert "0.5" in result and "0.9" in result
    assert get_score("test/file.md") == 0.9


def test_set_score_invalid(temp_state_file, reset_state):
    """Test setting invalid score."""
    from gptme_attention_router.tools.attention_router import set_score

    result = set_score("nonexistent/file.md", 0.5)
    assert "Error" in result


def test_reset_state(temp_state_file, reset_state):
    """Test resetting state."""
    from gptme_attention_router.tools.attention_router import (
        register_file,
        reset_state as do_reset,
        get_status,
    )

    register_file("test/file.md")
    do_reset()

    status = get_status()
    assert status["total_tracked"] == 0


def test_tool_spec_exists():
    """Test that tool spec is properly defined."""
    from gptme_attention_router.tools.attention_router import tool

    assert tool.name == "attention_router"
    assert tool.functions is not None
    assert len(tool.functions) == 10  # All functions registered
