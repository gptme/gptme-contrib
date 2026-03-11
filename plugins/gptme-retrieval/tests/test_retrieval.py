"""Tests for gptme-retrieval plugin."""

from unittest.mock import MagicMock, patch

from gptme.message import Message
from gptme_retrieval import (
    DEFAULT_CONFIG,
    get_retrieval_config,
    retrieve_context,
    turn_pre_hook,
)


def test_get_retrieval_config_defaults():
    """Test that defaults are returned when no config exists."""
    with patch("gptme_retrieval.get_config") as mock_config:
        mock_config.return_value = MagicMock(project=None, user=None)
        config = get_retrieval_config()
        assert config == DEFAULT_CONFIG


def test_retrieve_context_qmd_not_found():
    """Test graceful handling when qmd is not installed."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        results = retrieve_context("test query", backend="qmd")
        assert results == []


def test_retrieve_context_grep():
    """Test grep backend returns file matches."""
    mock_result = MagicMock()
    mock_result.stdout = "file1.md\nfile2.md\n"
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        results = retrieve_context("test", backend="grep", max_results=2)
        assert len(results) == 2
        assert results[0]["source"] == "file1.md"


def test_retrieve_context_threshold_filtering():
    """Test that results below threshold are filtered."""
    mock_result = MagicMock()
    mock_result.stdout = (
        '[{"content": "high", "score": 0.9}, {"content": "low", "score": 0.1}]'
    )
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        results = retrieve_context("test", backend="qmd", threshold=0.5)
        assert len(results) == 1
        assert results[0]["content"] == "high"


def test_turn_pre_hook_no_user_message():
    """Test that turn_pre_hook does nothing when no user message exists."""
    manager = MagicMock()
    manager.log.messages = []

    with patch("gptme_retrieval.get_retrieval_config", return_value=DEFAULT_CONFIG):
        messages = list(turn_pre_hook(manager))
    assert messages == []


def test_turn_pre_hook_yields_context():
    """Test that turn_pre_hook injects retrieved context as a system message."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="explain Thompson sampling")]

    mock_qmd_result = MagicMock()
    mock_qmd_result.returncode = 0
    mock_qmd_result.stdout = '[{"content": "Thompson sampling is a Bayesian approach", "path": "lessons/ts.md", "score": 0.9}]'

    config = {**DEFAULT_CONFIG, "backend": "qmd", "mode": "search", "threshold": 0.3}

    with (
        patch("gptme_retrieval.get_retrieval_config", return_value=config),
        patch("subprocess.run", return_value=mock_qmd_result),
    ):
        messages = list(turn_pre_hook(manager))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Thompson sampling" in messages[0].content


def test_turn_pre_hook_disabled():
    """Test that turn_pre_hook does nothing when disabled in config."""
    manager = MagicMock()
    manager.log.messages = [Message(role="user", content="test")]

    config = {**DEFAULT_CONFIG, "enabled": False}

    with patch("gptme_retrieval.get_retrieval_config", return_value=config):
        messages = list(turn_pre_hook(manager))
    assert messages == []
