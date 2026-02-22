"""Tests for gptme-retrieval plugin."""

from unittest.mock import MagicMock, patch

from gptme_retrieval import DEFAULT_CONFIG, get_retrieval_config, retrieve_context


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
