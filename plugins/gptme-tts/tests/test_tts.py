import asyncio
import importlib
import sys
import types
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
        patch("gptme_tts.tts.wait_for_audio") as mock_wait_audio,
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


# --- Backend teardown contract tests ---


def _import_tts_kokoro():
    kokoro_stub = types.ModuleType("kokoro")
    kokoro_stub.KPipeline = type("DummyPipeline", (), {})
    kokoro_stub.__version__ = "test"

    previous = sys.modules.get("kokoro")
    sys.modules.pop("tts_kokoro", None)
    sys.modules["kokoro"] = kokoro_stub
    try:
        return importlib.import_module("tts_kokoro")
    finally:
        if previous is None:
            sys.modules.pop("kokoro", None)
        else:
            sys.modules["kokoro"] = previous


def _import_tts_chatterbox():
    gradio_client_stub = types.ModuleType("gradio_client")

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    gradio_client_stub.Client = DummyClient
    gradio_client_stub.handle_file = lambda path: path

    previous = sys.modules.get("gradio_client")
    sys.modules.pop("tts_chatterbox", None)
    sys.modules["gradio_client"] = gradio_client_stub
    try:
        return importlib.import_module("tts_chatterbox")
    finally:
        if previous is None:
            sys.modules.pop("gradio_client", None)
        else:
            sys.modules["gradio_client"] = previous


def _import_tts_server():
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_responses_stub = types.ModuleType("fastapi.responses")

    class DummyFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class DummyHTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class DummyStreamingResponse:
        def __init__(self, *args, **kwargs):
            pass

    fastapi_stub.FastAPI = DummyFastAPI
    fastapi_stub.HTTPException = DummyHTTPException
    fastapi_responses_stub.StreamingResponse = DummyStreamingResponse

    previous_fastapi = sys.modules.get("fastapi")
    previous_fastapi_responses = sys.modules.get("fastapi.responses")
    sys.modules.pop("tts_server", None)
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = fastapi_responses_stub
    try:
        return importlib.import_module("tts_server")
    finally:
        if previous_fastapi is None:
            sys.modules.pop("fastapi", None)
        else:
            sys.modules["fastapi"] = previous_fastapi
        if previous_fastapi_responses is None:
            sys.modules.pop("fastapi.responses", None)
        else:
            sys.modules["fastapi.responses"] = previous_fastapi_responses


def test_kokoro_backend_close_contract():
    """KokoroTTSBackend.close() exists, is idempotent, and does not raise."""
    module = _import_tts_kokoro()

    with patch.object(module.KokoroTTSBackend, "_check_espeak", return_value=None):
        backend = module.KokoroTTSBackend(lang_code="a", voice="af_heart")

    backend.pipeline = object()
    backend.close()
    assert backend.pipeline is None

    backend.close()
    assert backend.pipeline is None


def test_chatterbox_backend_close_contract():
    """ChatterboxTTSBackend.close() exists, is idempotent, and does not raise."""
    module = _import_tts_chatterbox()

    with patch.dict("os.environ", {"HF_TOKEN": "test-token"}):
        backend = module.ChatterboxTTSBackend(voice_sample_dir="/tmp")
        backend.close()

    client = MagicMock()
    backend.client = client
    backend.close()
    client.close.assert_called_once()
    assert backend.client is None

    backend.close()
    client.close.assert_called_once()


def test_lifespan_shutdown_calls_close():
    """FastAPI lifespan shutdown path calls close() on the backend."""
    module = _import_tts_server()

    backend = MagicMock()

    async def exercise_lifespan():
        module.current_backend = None
        module.backend_name = "kokoro"
        async with module.lifespan(module.app):
            assert module.current_backend is backend

        backend.close.assert_called_once()
        assert module.current_backend is None

    with patch.object(
        module.TTSBackendLoader,
        "load_kokoro_backend",
        return_value=backend,
    ) as load_backend:
        asyncio.run(exercise_lifespan())

    load_backend.assert_called_once_with(lang_code="a", voice="af_heart")


def test_list_voices_kittentts_uses_tts_model_env_var():
    """The temporary kittentts backend should honor TTS_MODEL for --list-voices."""
    module = _import_tts_server()
    temp_backend = MagicMock()
    temp_backend.list_voices.return_value = ["Jasper"]

    with (
        patch.dict("os.environ", {"TTS_MODEL": "KittenML/kitten-tts-mini-0.8"}),
        patch.object(
            module.TTSBackendLoader,
            "load_kittentts_backend",
            return_value=temp_backend,
        ) as load_backend,
        patch("click.echo"),
    ):
        module.main.callback(
            port=8765,
            host="127.0.0.1",
            backend="kittentts",
            voice=None,
            lang="a",
            voice_dir=None,
            list_voices=True,
            list_backends=False,
            verbose=False,
        )

    load_backend.assert_called_once_with(
        model="KittenML/kitten-tts-mini-0.8",
        voice="Jasper",
    )
    temp_backend.close.assert_called_once()


def test_unavailable_kittentts_backend_prints_install_hint():
    """Unavailable kittentts backend should tell users how to install it."""
    module = _import_tts_server()

    with (
        patch.object(
            module.TTSBackendLoader, "get_available_backends", return_value=[]
        ),
        patch("click.echo") as echo,
        pytest.raises(SystemExit) as excinfo,
    ):
        module.main.callback(
            port=8765,
            host="127.0.0.1",
            backend="kittentts",
            voice=None,
            lang="a",
            voice_dir=None,
            list_voices=False,
            list_backends=False,
            verbose=False,
        )

    assert excinfo.value.code == 1
    echo.assert_any_call("Backend 'kittentts' is not available.", err=True)
    echo.assert_any_call("Available backends: []", err=True)
    echo.assert_any_call(
        f"Install KittenTTS: pip install {module.KITTENTTS_WHEEL_URL}",
        err=True,
    )
    echo.assert_any_call("Also install: pip install soundfile numpy scipy", err=True)
