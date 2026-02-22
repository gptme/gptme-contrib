"""Tests for cost tracking functionality."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from gptme_imagen.tools.cost_tracker import PROVIDER_COSTS, CostTracker


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_costs.db"
        yield db_path


@pytest.fixture
def tracker(temp_db):
    """Create a cost tracker instance with temporary database."""
    return CostTracker(db_path=temp_db)


def test_cost_tracker_init(temp_db):
    """Test cost tracker initialization creates database and tables."""
    _ = CostTracker(db_path=temp_db)
    assert temp_db.exists()

    # Verify tables exist
    with sqlite3.connect(temp_db) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='generations'"
        )
        assert cursor.fetchone() is not None


def test_calculate_cost_gemini(tracker):
    """Test cost calculation for Gemini provider."""
    # Standard quality
    cost = tracker.calculate_cost(provider="gemini", quality="standard", count=1)
    assert cost == PROVIDER_COSTS["gemini"]["imagen-3-fast"]

    # Multiple images
    cost = tracker.calculate_cost(provider="gemini", quality="standard", count=3)
    assert cost == PROVIDER_COSTS["gemini"]["imagen-3-fast"] * 3


def test_calculate_cost_dalle(tracker):
    """Test cost calculation for DALL-E providers."""
    # DALL-E 3 standard
    cost = tracker.calculate_cost(provider="dalle", quality="standard", count=1)
    assert cost == PROVIDER_COSTS["dalle"]["standard"]

    # DALL-E 3 HD
    cost = tracker.calculate_cost(provider="dalle", quality="hd", count=1)
    assert cost == PROVIDER_COSTS["dalle"]["hd"]

    # DALL-E 2
    cost = tracker.calculate_cost(provider="dalle2", quality="standard", count=1)
    assert cost == PROVIDER_COSTS["dalle2"]["standard"]


def test_calculate_cost_unknown_provider(tracker):
    """Test cost calculation for unknown provider returns 0."""
    cost = tracker.calculate_cost(provider="unknown", quality="standard", count=1)
    assert cost == 0.0


def test_record_generation(tracker):
    """Test recording a generation."""
    record_id = tracker.record_generation(
        provider="gemini",
        prompt="Test prompt",
        cost=0.04,
        model="imagen-3-fast",
        size="1024x1024",
        quality="standard",
        count=1,
        output_path="/tmp/test.png",
    )

    assert record_id > 0

    # Verify record exists
    history = tracker.get_generation_history(limit=1)
    assert len(history) == 1
    assert history[0]["provider"] == "gemini"
    assert history[0]["prompt"] == "Test prompt"
    assert history[0]["cost_usd"] == 0.04


def test_get_total_cost(tracker):
    """Test getting total cost."""
    # Record some generations
    tracker.record_generation("gemini", "Test 1", 0.04)
    tracker.record_generation("dalle", "Test 2", 0.08)
    tracker.record_generation("dalle2", "Test 3", 0.02)

    # Get total cost
    total = tracker.get_total_cost()
    assert total == 0.14

    # Filter by provider
    gemini_cost = tracker.get_total_cost(provider="gemini")
    assert gemini_cost == 0.04


def test_get_total_cost_with_dates(tracker):
    """Test getting total cost with date filtering."""
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).isoformat()
    tomorrow = (now + timedelta(days=1)).isoformat()

    # Record generation
    tracker.record_generation("gemini", "Test", 0.04)

    # Cost within date range
    cost = tracker.get_total_cost(start_date=yesterday, end_date=tomorrow)
    assert cost == 0.04

    # Cost outside date range (future)
    future = (now + timedelta(days=2)).isoformat()
    cost = tracker.get_total_cost(start_date=future)
    assert cost == 0.0


def test_get_cost_breakdown(tracker):
    """Test getting cost breakdown by provider."""
    # Record generations
    tracker.record_generation("gemini", "Test 1", 0.04)
    tracker.record_generation("gemini", "Test 2", 0.04)
    tracker.record_generation("dalle", "Test 3", 0.08)
    tracker.record_generation("dalle2", "Test 4", 0.02)

    breakdown = tracker.get_cost_breakdown()
    assert breakdown["gemini"] == 0.08
    assert breakdown["dalle"] == 0.08
    assert breakdown["dalle2"] == 0.02


def test_get_generation_history(tracker):
    """Test getting generation history."""
    # Record generations
    tracker.record_generation("gemini", "Test 1", 0.04, model="imagen-3-fast")
    tracker.record_generation("dalle", "Test 2", 0.08, model="dall-e-3")

    history = tracker.get_generation_history(limit=10)
    assert len(history) == 2

    # Most recent first
    assert history[0]["prompt"] == "Test 2"
    assert history[1]["prompt"] == "Test 1"

    # Filter by provider
    history = tracker.get_generation_history(limit=10, provider="gemini")
    assert len(history) == 1
    assert history[0]["prompt"] == "Test 1"


def test_get_generation_history_limit(tracker):
    """Test generation history respects limit."""
    # Record many generations
    for i in range(10):
        tracker.record_generation("gemini", f"Test {i}", 0.04)

    history = tracker.get_generation_history(limit=5)
    assert len(history) == 5


def test_cost_tracker_singleton():
    """Test get_cost_tracker returns singleton instance."""
    from gptme_imagen.tools.cost_tracker import get_cost_tracker

    tracker1 = get_cost_tracker()
    tracker2 = get_cost_tracker()
    assert tracker1 is tracker2


def test_record_multiple_images(tracker):
    """Test recording generation with multiple images."""
    record_id = tracker.record_generation(
        provider="gemini",
        prompt="Generate 3 variations",
        cost=0.12,  # 0.04 * 3
        count=3,
    )

    assert record_id > 0

    history = tracker.get_generation_history(limit=1)
    assert history[0]["count"] == 3
    assert history[0]["cost_usd"] == 0.12
