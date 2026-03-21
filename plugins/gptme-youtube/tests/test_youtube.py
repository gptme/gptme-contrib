"""Tests for the YouTube plugin."""

from unittest.mock import MagicMock, patch


def test_tool_spec():
    """Test that the tool spec is properly defined."""
    from gptme_youtube.tools.youtube import tool

    assert tool.name == "youtube"
    assert "youtube" in tool.block_types
    assert len(tool.functions) == 2


def test_get_transcript_no_library():
    """Test get_transcript when youtube_transcript_api is not installed."""
    with patch("gptme_youtube.tools.youtube.YouTubeTranscriptApi", None):
        from gptme_youtube.tools.youtube import get_transcript

        result = get_transcript("test_id")
        assert "not installed" in result


def test_get_transcript_success():
    """Test get_transcript with a mocked API."""
    mock_api = MagicMock()
    mock_api.get_transcript.return_value = [
        {"text": "Hello"},
        {"text": "world"},
    ]

    with patch("gptme_youtube.tools.youtube.YouTubeTranscriptApi", mock_api):
        from gptme_youtube.tools.youtube import get_transcript

        result = get_transcript("test_id")
        assert result == "Hello world"
        mock_api.get_transcript.assert_called_once_with("test_id")


def test_get_transcript_error():
    """Test get_transcript when API raises an exception."""
    mock_api = MagicMock()
    mock_api.get_transcript.side_effect = Exception("Video not found")

    with patch("gptme_youtube.tools.youtube.YouTubeTranscriptApi", mock_api):
        from gptme_youtube.tools.youtube import get_transcript

        result = get_transcript("bad_id")
        assert "Error fetching transcript" in result
