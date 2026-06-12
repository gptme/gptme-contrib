"""Pytest configuration for gptme_imagen tests."""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Pre-populate sys.modules with mock stubs for optional dependencies.
# This allows tests to patch these modules (e.g., mock.patch("gptme.tools.vision.view_image"))
# even when the actual packages are not installed (gptme, openai, requests are optional deps).
#
# IMPORTANT: mock.patch("a.b.c.attr") resolves the target by importing "a" and then
# traversing .b.c via attribute access — NOT via sys.modules["a.b.c"] directly.
# So the mock objects must be properly linked: sys.modules["a"].b must be sys.modules["a.b"].
if "gptme" not in sys.modules:
    _gptme = MagicMock()
    _gptme_tools = MagicMock()
    _gptme_tools_vision = MagicMock()
    _gptme_tools_base = MagicMock()
    _gptme_tools_python = MagicMock()
    _gptme_config = MagicMock()
    _gptme_message = MagicMock()
    _gptme_hooks = MagicMock()
    _gptme_constants = MagicMock()

    # Link the hierarchy so attribute traversal works correctly
    _gptme.tools = _gptme_tools
    _gptme_tools.vision = _gptme_tools_vision
    _gptme_tools.base = _gptme_tools_base
    _gptme_tools.python = _gptme_tools_python
    _gptme.config = _gptme_config
    _gptme.message = _gptme_message
    _gptme.hooks = _gptme_hooks
    _gptme.constants = _gptme_constants

    # Set up ConfirmAction enum-like mock
    _confirm_action = MagicMock()
    _confirm_action.CONFIRM = "confirm"
    _gptme_hooks.ConfirmAction = _confirm_action

    # Set up DECLINED_CONTENT
    _gptme_constants.DECLINED_CONTENT = "Declined."

    # Set up a minimal Message class (not MagicMock) so isinstance() works
    class _MessageStub:
        def __init__(self, role: str, content: str, files=None, **kwargs):
            self.role = role
            self.content = content
            self.files = files

    _gptme_message.Message = _MessageStub

    sys.modules["gptme"] = _gptme
    sys.modules["gptme.tools"] = _gptme_tools
    sys.modules["gptme.tools.vision"] = _gptme_tools_vision
    sys.modules["gptme.tools.base"] = _gptme_tools_base
    sys.modules["gptme.tools.python"] = _gptme_tools_python
    sys.modules["gptme.config"] = _gptme_config
    sys.modules["gptme.message"] = _gptme_message
    sys.modules["gptme.hooks"] = _gptme_hooks
    sys.modules["gptme.constants"] = _gptme_constants

if "openai" not in sys.modules:
    sys.modules["openai"] = MagicMock()

if "requests" not in sys.modules:
    sys.modules["requests"] = MagicMock()


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
