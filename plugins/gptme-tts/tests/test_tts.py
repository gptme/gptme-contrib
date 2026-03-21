from unittest.mock import MagicMock, patch

import pytest


def _has_gptme():
    try:
        import gptme  # noqa: F401

        return True
    except ImportError:
        return False


def test_tool_spec():
    """Test that the tool spec is properly defined."""
    from gptme_tts.tts import tool

    assert tool.name == "tts"
    assert tool.desc
    assert tool.functions
    assert tool.hooks


def test_split_text_single_sentence():
    from gptme_tts.tts import split_text

    assert split_text("Hello, world!") == ["Hello, world!"]


def test_split_text_multiple_sentences():
    from gptme_tts.tts import split_text

    assert split_text("Hello, world! I'm Bob") == ["Hello, world!", "I'm Bob"]


def test_split_text_decimals():
    from gptme_tts.tts import split_text

    result = split_text("0.5x")
    assert result == ["0.5x"]


def test_split_text_numbers_before_punctuation():
    from gptme_tts.tts import split_text

    assert split_text("The dog was 12. The cat was 3.") == [
        "The dog was 12.",
        "The cat was 3.",
    ]


def test_split_text_paragraphs():
    from gptme_tts.tts import split_text

    assert split_text(
        """
Text without punctuation

Another paragraph
"""
    ) == ["Text without punctuation", "", "Another paragraph"]


def test_join_short_sentences():
    from gptme_tts.tts import join_short_sentences

    # Test basic sentence joining
    sentences: list[str] = ["Hello.", "World."]
    result = join_short_sentences(sentences, min_length=100)
    assert result == ["Hello. World."]

    # Test with min_length to force splits
    sentences = ["One two", "three four", "five."]
    result = join_short_sentences(sentences, min_length=10)
    assert result == ["One two three four five."]

    # Test with max_length to limit combining
    result = join_short_sentences(sentences, min_length=10, max_length=20)
    assert result == ["One two three four", "five."]

    # Test with empty lines (should preserve paragraph breaks)
    sentences = ["Hello.", "", "World."]
    result = join_short_sentences(sentences, min_length=100)
    assert result == ["Hello.", "", "World."]

    # Test with multiple sentences and punctuation
    sentences = ["First.", "Second!", "Third?", "Fourth."]
    result = join_short_sentences(sentences, min_length=100)
    assert result == ["First. Second! Third? Fourth."]


def test_split_text_lists():
    from gptme_tts.tts import split_text

    assert split_text(
        """
- Test
- Test2
"""
    ) == ["- Test", "- Test2"]

    # Markdown list (numbered)
    assert split_text(
        """
1. Test.
2. Test2
"""
    ) == ["1. Test.", "2. Test2"]

    # We can strip trailing punctuation from list items
    assert [
        part.strip()
        for part in split_text(
            """
1. Test.
2. Test2.
"""
        )
    ] == ["1. Test", "2. Test2"]

    # Replace asterisk lists with dashes
    assert split_text(
        """
* Test
* Test2
"""
    ) == ["- Test", "- Test2"]


def test_clean_for_speech():
    from gptme_tts.tts import re_thinking, re_tool_use

    # complete
    assert re_thinking.search("<thinking>thinking</thinking>")
    assert re_tool_use.search("```tool\ncontents\n```")

    # with arg
    assert re_tool_use.search("```save ~/path_to/test-file1.txt\ncontents\n```")

    # with `text` contents
    assert re_tool_use.search("```file.md\ncontents with `code` string\n```")

    # incomplete
    assert re_thinking.search("\n<thinking>thinking")
    assert re_tool_use.search("```savefile.txt\ncontents")

    # make sure spoken content is correct
    assert (
        re_tool_use.sub("", "Using tool\n```tool\ncontents\n```").strip()
        == "Using tool"
    )
    assert re_tool_use.sub("", "```tool\ncontents\n```\nRan tool").strip() == "Ran tool"


def test_hooks_registered():
    """Test that TTS hooks are properly registered in the tool spec."""
    from gptme_tts.tts import tool

    assert "speak_on_generation" in tool.hooks
    assert "wait_on_session_end" in tool.hooks

    # Check hook types
    assert tool.hooks["speak_on_generation"][0] == "generation_post"
    assert tool.hooks["wait_on_session_end"][0] == "session_end"


@pytest.mark.skipif(not _has_gptme(), reason="gptme not installed")
def test_speak_on_generation_hook():
    """Test that speak_on_generation hook calls speak() for assistant messages."""
    from gptme.message import Message
    from gptme_tts.tts import speak_on_generation

    with patch("gptme_tts.tts.speak") as mock_speak:
        # Test with assistant message
        msg = Message("assistant", "Hello, world!")
        result = list(speak_on_generation(message=msg))

        mock_speak.assert_called_once_with("Hello, world!")
        assert len(result) == 1
        assert result[0] is None

        mock_speak.reset_mock()

        # Test with non-assistant message (should not speak)
        user_msg = Message("user", "Test user message")
        result = list(speak_on_generation(message=user_msg))
        mock_speak.assert_not_called()


@pytest.mark.skipif(not _has_gptme(), reason="gptme not installed")
def test_wait_on_session_end_hook():
    """Test that wait_on_session_end hook waits for TTS when enabled."""
    from gptme_tts.tts import wait_on_session_end

    with (
        patch("gptme_tts.tts.tts_request_queue") as mock_queue,
        patch("gptme_tts.tts.stop"),
        patch("gptme_tts.tts.os.environ.get", return_value="1"),
        patch("gptme.util.sound.wait_for_audio") as mock_wait_audio,
    ):
        mock_manager = MagicMock()
        result = list(wait_on_session_end(manager=mock_manager))

        mock_queue.join.assert_called_once()
        mock_wait_audio.assert_called_once()
        assert len(result) == 1
        assert result[0] is None


@pytest.mark.skipif(not _has_gptme(), reason="gptme not installed")
def test_wait_on_session_end_disabled():
    """Test that wait_on_session_end hook does nothing when disabled."""
    from gptme_tts.tts import wait_on_session_end

    with patch("gptme_tts.tts.os.environ.get", return_value="0"):
        mock_manager = MagicMock()

        with patch("gptme_tts.tts.tts_request_queue") as mock_queue:
            result = list(wait_on_session_end(manager=mock_manager))

        mock_queue.join.assert_not_called()
        assert len(result) == 0
