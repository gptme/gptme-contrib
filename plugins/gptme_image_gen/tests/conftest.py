"""Pytest configuration for gptme_image_gen tests."""

import os

import pytest


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow (integration tests)")
    config.addinivalue_line("markers", "requires_api_keys: requires API keys to run")


@pytest.fixture
def mock_api_keys(monkeypatch):
    """Mock API keys for testing without real credentials."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-google")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-openai")


@pytest.fixture
def temp_output_dir(tmp_path):
    """Create a temporary directory for test outputs."""
    output_dir = tmp_path / "test_images"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def skip_if_no_api_keys():
    """Skip tests if required API keys are not available."""
    if not os.getenv("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY not set")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
