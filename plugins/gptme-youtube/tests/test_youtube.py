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


def test_execute_with_code():
    """Test execute extracts video ID from the code block content."""
    mock_api = MagicMock()
    mock_api.get_transcript.return_value = [{"text": "Hello world"}]

    with patch("gptme_youtube.tools.youtube.YouTubeTranscriptApi", mock_api):
        from gptme_youtube.tools.youtube import execute

        msg = execute("dQw4w9WgXcQ", None, None)
        assert "Hello world" in msg.content
        mock_api.get_transcript.assert_called_once_with("dQw4w9WgXcQ")


def test_execute_fallback_to_args():
    """Test execute falls back to args when code is empty."""
    mock_api = MagicMock()
    mock_api.get_transcript.return_value = [{"text": "Fallback transcript"}]

    with patch("gptme_youtube.tools.youtube.YouTubeTranscriptApi", mock_api):
        from gptme_youtube.tools.youtube import execute

        msg = execute("", ["dQw4w9WgXcQ"], None)
        assert "Fallback transcript" in msg.content
        mock_api.get_transcript.assert_called_once_with("dQw4w9WgXcQ")


def test_execute_empty_video_id():
    """Test execute returns an error when no video ID is provided."""
    from gptme_youtube.tools.youtube import execute

    msg = execute("", None, None)
    assert "Error" in msg.content


def test_extract_video_id_from_url():
    """Test _extract_video_id handles full YouTube URLs and bare IDs."""
    from gptme_youtube.tools.youtube import _extract_video_id

    assert _extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert (
        _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )
    assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ?si=abc") == "dQw4w9WgXcQ"
    assert (
        _extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    )
