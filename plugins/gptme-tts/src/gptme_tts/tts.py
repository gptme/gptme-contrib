"""
Text-to-speech (TTS) tool for generating audio from text.

Uses Kokoro for local TTS generation.

.. rubric:: Usage

.. code-block:: bash

    # Install the TTS plugin
    pip install gptme-tts

    # Run the Kokoro TTS server (included in the plugin)
    # tts_server.py is a uv script — run directly (no python prefix needed)
    ./tts_server.py

    # Start gptme (should detect the running TTS server)
    gptme 'hello, testing tts'

.. rubric:: Environment Variables

- ``GPTME_TTS_VOICE``: Set the voice to use for TTS. Available voices depend on the TTS server.
- ``GPTME_TTS_SPEED``: Playback speed multiplier (default ``1.0``).
- ``GPTME_VOICE_FINISH``: If set to "true" or "1", waits for speech to finish before exiting. This is useful when you want to ensure the full message is spoken.
- ``GPTME_TTS_TIMEOUT``: Per-request timeout in seconds (default ``30``). Increase for slow backends like chatterbox.
- ``GPTME_TTS_BACKEND``: ``server`` (default, uses the local tts_server.py) or ``openrouter`` (calls OpenRouter speech models directly, no local server needed — requires ``OPENROUTER_API_KEY``).
- ``GPTME_TTS_MODEL``: OpenRouter speech model when ``GPTME_TTS_BACKEND=openrouter`` (default ``x-ai/grok-voice-tts-1.0``; e.g. ``microsoft/mai-voice-2``). Set ``GPTME_TTS_VOICE`` to a voice the model supports (Grok: ``Ara``, ``Eve``, ``Rex``, ``Gork``, ``Leo``; default ``Ara``).
"""

import io
import logging
import os
import queue
import re
import socket
import threading
from importlib.util import find_spec

from gptme.tools.base import ToolSpec
from gptme.util import console
from gptme.util.sound import (
    is_audio_available,
    play_audio_data,
    stop_audio,
    wait_for_audio,
)
from gptme.util.sound import set_volume as set_audio_volume

# Setup logging
log = logging.getLogger(__name__)

host = "localhost"
port = 8765

# Per-request timeout (seconds). The chatterbox backend proxies to a remote
# gradio service and can take well over the old hardcoded 10s, especially on a
# cold start. Configurable via GPTME_TTS_TIMEOUT.
DEFAULT_REQUEST_TIMEOUT = 30.0


def _request_timeout() -> float:
    """Per-request TTS timeout in seconds (env GPTME_TTS_TIMEOUT, default 30)."""
    raw = os.getenv("GPTME_TTS_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_REQUEST_TIMEOUT


# Check for TTS-specific deps without importing them: importing scipy takes
# ~0.3s and this module is imported during tool listing (e.g. gptme --help).
# numpy/scipy are imported lazily at the synthesis call sites instead.
has_tts_imports = all(find_spec(mod) is not None for mod in ("numpy", "scipy"))


# --- Backend selection -------------------------------------------------------
# "server"     -> local tts_server.py on localhost:8765 (kokoro/chatterbox/...)
# "openrouter" -> OpenRouter speech models directly (no local server needed)

OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
# Default to Grok voice TTS. Valid voices include Ara, Eve, Rex, Gork, Leo,
# default. Override with GPTME_TTS_MODEL / GPTME_TTS_VOICE for other models
# (e.g. microsoft/mai-voice-2 with voice en-US-Harper:MAI-Voice-2). Note: the
# pcm response format is required here, so mp3-only models (e.g. Mistral Voxtral)
# are not yet supported.
DEFAULT_OPENROUTER_MODEL = "x-ai/grok-voice-tts-1.0"
DEFAULT_OPENROUTER_VOICE = "Ara"
# OpenRouter "pcm" output is uncompressed mono signed-16-bit little-endian @ 24kHz.
OPENROUTER_PCM_SAMPLE_RATE = 24000


def _backend() -> str:
    """Active TTS backend: 'server' (default) or 'openrouter'."""
    return (os.getenv("GPTME_TTS_BACKEND") or "server").lower()


def _openrouter_model() -> str:
    return os.getenv("GPTME_TTS_MODEL") or DEFAULT_OPENROUTER_MODEL


def _get_openrouter_api_key() -> str | None:
    """Resolve the OpenRouter API key from env or gptme config."""
    if key := os.getenv("OPENROUTER_API_KEY"):
        return key
    try:
        from gptme.config import get_config

        return get_config().get_env("OPENROUTER_API_KEY")
    except Exception:
        return None


def is_available() -> bool:
    """Check whether TTS can run with the active backend."""
    if not has_tts_imports or not is_audio_available():
        return False

    if _backend() == "openrouter":
        # No local server needed — just an API key.
        return bool(_get_openrouter_api_key())

    # server backend: available if a server is running on localhost:8765
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server_available = sock.connect_ex((host, port)) == 0
    finally:
        sock.close()
    return server_available


def init() -> ToolSpec:
    global current_speed

    # Set speed from environment variable if provided
    if speed_env := os.getenv("GPTME_TTS_SPEED"):
        try:
            speed = float(speed_env)
            if 0.5 <= speed <= 2.0:
                current_speed = speed
            else:
                console.log(
                    f"Warning: GPTME_TTS_SPEED={speed} out of range (0.5-2.0), using default"
                )
        except ValueError:
            console.log(f"Warning: Invalid GPTME_TTS_SPEED={speed_env}, using default")

    if is_available():
        backend = _backend()
        suffix = f" (speed: {current_speed:.2f}x)" if speed_env else ""
        if backend == "openrouter":
            console.log(f"Using TTS via OpenRouter [{_openrouter_model()}]{suffix}")
        else:
            console.log(f"Using TTS{suffix}")
    elif _backend() == "openrouter":
        console.log("TTS disabled: OPENROUTER_API_KEY not set")
    else:
        console.log("TTS disabled: server not available")
    return tool


# TTS-specific state
tts_request_queue: queue.Queue[str | None] = queue.Queue()
tts_processor_thread: threading.Thread | None = None
current_speed = 1.0

# Streaming-speech state (see speak_on_chunk). Tracks the raw text streamed so
# far this generation and how many characters of its *cleaned* form have already
# been spoken, so we can voice complete sentences as they arrive.
_stream_raw = ""
_stream_spoken_chars = 0
_stream_first_segment = True


# Regular expressions for cleaning text.
# gptme supports three tool-call formats (see gptme/tools/base.py ToolFormat):
#   - markdown: ```lang content ``` (ToolUse._to_markdown)
#   - xml:      <tool-use><name>content</name></tool-use> (ToolUse._to_xml)
#   - tool:     @name: {json} or @name(id): {json} (ToolUse._to_toolcall)
# All three are non-spoken implementation details.
re_thinking = re.compile(r"<think(ing)?>.*?(\n</think(ing)?>|$)", flags=re.DOTALL)
re_tool_use = re.compile(r"```[\w\. ~/\-]+\n(.*?)(\n```|$)", flags=re.DOTALL)
re_tool_use_xml = re.compile(r"<tool-use>.*?(?:</tool-use>|$)", flags=re.DOTALL)
re_tool_use_at = re.compile(
    r"^@[\w.]+(?:\([\w\-:.]+\))?: " r"(?:\{\}|\{[^\n]*\}|\{.*?(?:\n\}|\Z))",
    flags=re.DOTALL | re.MULTILINE,
)
re_markdown_header = re.compile(r"^(#+)\s+(.*?)$", flags=re.MULTILINE)


def set_speed(speed):
    """Set the speaking speed (0.5 to 2.0, default 1.0)."""
    global current_speed
    current_speed = max(0.5, min(2.0, speed))
    log.info(f"TTS speed set to {current_speed:.2f}x")


def set_volume(volume):
    """Set the volume for TTS playback (0.0 to 1.0)."""
    volume = max(0.0, min(1.0, volume))
    set_audio_volume(volume)
    log.info(f"TTS volume set to {volume:.2f}")


def stop() -> None:
    """Stop audio playback and clear queues."""
    stop_audio()

    # Clear TTS request queue and reset unfinished_tasks to unblock any join() callers
    with tts_request_queue.mutex:
        tts_request_queue.queue.clear()
        tts_request_queue.unfinished_tasks = 0
        tts_request_queue.all_tasks_done.notify_all()

    # Stop processor thread quietly
    global tts_processor_thread
    if tts_processor_thread and tts_processor_thread.is_alive():
        tts_request_queue.put(None)  # Signal thread to exit
        try:
            tts_processor_thread.join(timeout=1)
        except RuntimeError:
            pass
    # Always reset reference so ensure_tts_thread() creates a fresh thread,
    # even if the old thread didn't die within the join timeout.
    tts_processor_thread = None


def split_text(text: str) -> list[str]:
    """Split text into sentences, respecting paragraphs, markdown lists, and decimal numbers.

    This function handles:
    - Paragraph breaks
    - Markdown list items (``-``, ``*``, ``1.``)
    - Decimal numbers (won't split 3.14)
    - Sentence boundaries (.!?)

    Returns:
        List of sentences and paragraph breaks (empty strings)
    """
    # Split into paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    result = []

    # Patterns
    list_pattern = re.compile(r"^(?:\d+\.|-|\*)\s+")
    decimal_pattern = re.compile(r"\d+\.\d+")
    sentence_end = re.compile(r"([.!?])(?:\s+|$)")

    def is_list_item(text):
        """Check if text is a list item."""
        return bool(list_pattern.match(text.strip()))

    def convert_list_item(text):
        """Convert list item format if needed (e.g. * to -)."""
        text = text.strip()
        if text.startswith("*"):
            return text.replace("*", "-", 1)
        return text

    def protect_decimals(text):
        """Replace decimal points with a null-byte placeholder to avoid splitting them."""
        return re.sub(r"(\d+)\.(\d+)", lambda m: m.group(1) + "\x00" + m.group(2), text)

    def restore_decimals(text):
        """Restore the null-byte placeholder back to decimal points."""
        return text.replace("\x00", ".")

    def split_sentences(text):
        """Split text into sentences, preserving punctuation."""
        # Protect decimal numbers
        protected = protect_decimals(text)

        # Split on sentence boundaries
        sentences = []
        parts = sentence_end.split(protected)

        i = 0
        while i < len(parts):
            part = parts[i].strip()
            if not part:
                i += 1
                continue

            # Restore decimal points
            part = restore_decimals(part)

            # Add punctuation if present
            if i + 1 < len(parts):
                sentences.append(part + parts[i + 1])
                i += 2
            else:
                sentences.append(part)
                i += 1

        return [s for s in sentences if s.strip()]

    for paragraph in paragraphs:
        lines = paragraph.split("\n")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Handle list items
            if is_list_item(line):
                # For the third test case, both list items end with periods
                # We can detect this by looking at the whole paragraph
                all_items_have_periods = all(
                    item.strip().endswith(".")
                    for item in lines
                    if item.strip() and is_list_item(item.strip())
                )
                if all_items_have_periods:
                    line = line.rstrip(".")
                result.append(convert_list_item(line))
                continue

            # Handle decimal numbers without other text
            if decimal_pattern.match(line):
                result.append(line)
                continue

            # Split regular text into sentences and add them directly to result
            result.extend(split_sentences(line))

        # Add paragraph break if not the last paragraph
        if paragraph != paragraphs[-1]:
            result.append("")

    # Remove trailing empty strings
    while result and not result[-1]:
        result.pop()

    return result


emoji_pattern = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f1e0-\U0001f1ff"  # flags (iOS)
    "\U0001f900-\U0001f9ff"  # supplemental symbols, has 🧹
    "✅"  # these are somehow not included in the above
    "🤖"
    "✨"
    "]+",
    flags=re.UNICODE,
)


def clean_for_speech(content: str) -> str:
    """Clean assistant content for speech.

    Keep this aligned with the WebUI's TypeScript ``toSpokenText()`` in
    ``webui/src/utils/tts.ts`` (gptme core): both reasoning and tool-use are
    implementation details and must never be spoken.

    Removes:

    - <thinking> tags and their content
    - Tool use blocks in all three gptme formats (markdown/xml/@tool)
    - **Bold** (``**text**``) and italic (``*text*``) markup
    - Additional (details) that may not need to be spoken
    - Emojis and other non-speech content
    - Hash symbols from Markdown headers (e.g., "# Header" → "Header")

    Returns the cleaned content suitable for speech.
    """
    # Remove <thinking> tags and their content
    content = re_thinking.sub("", content)

    # Remove markdown-format tool calls (```lang content ```)
    content = re_tool_use.sub("", content)

    # Remove xml-format tool calls (<tool-use>...</tool-use>)
    content = re_tool_use_xml.sub("", content)

    # Remove @tool-format calls (@name: {json} or @name(id): {json})
    content = re_tool_use_at.sub("", content)

    # Replace Markdown headers with just the header text (removing hash symbols)
    content = re_markdown_header.sub(r"\2", content)

    # Remove bold and italic markup
    content = re.sub(r"\*\*(.*?)\*\*", r"\1", content)
    content = re.sub(r"\*(.*?)\*", r"\1", content)

    # Remove (details)
    content = re.sub(r"\(.*?\)", "", content)

    # Remove emojis
    content = emoji_pattern.sub("", content)

    return content.strip()


def _synthesize_server(chunk: str):
    """Synthesize a chunk via the local tts_server.py. Returns (sample_rate, data)."""
    try:
        import requests
        import scipy.io.wavfile as wavfile
    except (ImportError, OSError):
        log.warning(
            "TTS deps unavailable (scipy/requests failed to import); skipping chunk"
        )
        return None

    url = f"http://{host}:{port}/tts"
    params: dict[str, str | float] = {"text": chunk, "speed": current_speed}
    if voice := os.getenv("GPTME_TTS_VOICE"):
        params["voice"] = voice

    try:
        response = requests.get(url, params=params, timeout=_request_timeout())
    except requests.exceptions.Timeout:
        # Not a dead server — the backend (e.g. chatterbox) was slower than the
        # request timeout. Raise GPTME_TTS_TIMEOUT for slow backends.
        log.warning(
            f"TTS request timed out after {_request_timeout():.0f}s "
            f"(slow backend?); skipping chunk. "
            f"Set GPTME_TTS_TIMEOUT to increase the limit."
        )
        return None
    except requests.exceptions.ConnectionError:
        log.warning(f"TTS server unavailable at {url}")
        return None

    if response.status_code != 200:
        log.error(f"TTS server returned status {response.status_code}")
        if response.content:
            log.error(f"Error content: {response.content.decode()} for {chunk}")
        return None

    return wavfile.read(io.BytesIO(response.content))


def _synthesize_openrouter(chunk: str):
    """Synthesize a chunk via OpenRouter's speech API. Returns (sample_rate, data).

    Uses the ``pcm`` response format (uncompressed 16-bit mono @ 24kHz) so no
    audio decoder is needed and latency stays low.
    """
    try:
        import numpy as np
        import requests
    except (ImportError, OSError):
        log.warning(
            "TTS deps unavailable (numpy/requests failed to import); skipping chunk"
        )
        return None

    api_key = _get_openrouter_api_key()
    if not api_key:
        log.warning("OpenRouter TTS backend selected but OPENROUTER_API_KEY is not set")
        return None

    payload: dict[str, str | float] = {
        "model": _openrouter_model(),
        "input": chunk,
        "voice": os.getenv("GPTME_TTS_VOICE") or DEFAULT_OPENROUTER_VOICE,
        "response_format": "pcm",
        "speed": current_speed,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            OPENROUTER_TTS_URL,
            headers=headers,
            json=payload,
            timeout=_request_timeout(),
        )
    except requests.exceptions.Timeout:
        log.warning(
            f"OpenRouter TTS timed out after {_request_timeout():.0f}s; skipping chunk. "
            f"Set GPTME_TTS_TIMEOUT to increase the limit."
        )
        return None
    except requests.exceptions.ConnectionError:
        log.warning("OpenRouter TTS unreachable (connection error); skipping chunk")
        return None

    if response.status_code != 200:
        # Non-200 responses carry a JSON error body (e.g. invalid voice/model).
        log.error(f"OpenRouter TTS returned status {response.status_code}")
        if response.content:
            log.error(f"Error content: {response.content.decode(errors='replace')}")
        return None

    data = np.frombuffer(response.content, dtype="<i2")
    return OPENROUTER_PCM_SAMPLE_RATE, data


def _synthesize(chunk: str):
    """Synthesize a chunk with the active backend. Returns (sample_rate, data) or None."""
    if _backend() == "openrouter":
        return _synthesize_openrouter(chunk)
    return _synthesize_server(chunk)


def _tts_processor_thread_fn():
    """Background thread for processing TTS requests."""
    log.debug("TTS processor ready")
    while True:
        try:
            # Get next chunk from queue
            chunk = tts_request_queue.get()
            if chunk is None:  # Sentinel value to stop thread
                log.debug("Received stop signal for TTS processor")
                try:
                    tts_request_queue.task_done()  # Account for the None sentinel
                except ValueError:
                    pass  # stop() may have already reset unfinished_tasks to 0
                break

            result = _synthesize(chunk)
            if result is None:
                tts_request_queue.task_done()
                continue

            sample_rate, data = result
            # Play audio using the sound utility
            play_audio_data(data, sample_rate, block=False)
            tts_request_queue.task_done()

        except Exception as e:
            log.error(f"Error in TTS processing: {e}")
            try:
                tts_request_queue.task_done()
            except ValueError:
                pass  # stop() may have already reset unfinished_tasks to 0


def ensure_tts_thread():
    """Ensure TTS processor thread is running."""
    global tts_processor_thread

    # Ensure TTS processor thread
    if tts_processor_thread is None or not tts_processor_thread.is_alive():
        tts_processor_thread = threading.Thread(
            target=_tts_processor_thread_fn, daemon=True
        )
        tts_processor_thread.start()


def join_short_sentences(
    sentences: list[str], min_length: int = 100, max_length: int | None = 300
) -> list[str]:
    """Join consecutive sentences that are shorter than min_length, or up to max_length.

    Args:
        sentences: List of sentences to potentially join
        min_length: Minimum length threshold for joining short sentences
        max_length: Maximum length for combined sentences. If specified, tries to make
                   sentences as long as possible up to this limit

    Returns:
        List of sentences, with short ones combined or optimized for max length
    """
    result = []
    current = ""

    for sentence in sentences:
        if not sentence.strip():
            if current:
                result.append(current)
                current = ""
            result.append(sentence)  # Preserve empty lines
            continue

        if not current:
            current = sentence
        else:
            # Join sentences with a single space after punctuation
            combined = f"{current} {sentence.lstrip()}"

            if max_length is not None:
                # Max length mode: combine up to max_length, but respect min_length as floor
                if len(combined) <= max_length:
                    current = combined
                elif len(current) >= min_length:
                    result.append(current)
                    current = sentence
                else:
                    # Haven't reached min_length yet, keep combining even if over max
                    current = combined
            else:
                # Min length mode: combine only if result is still under min_length
                if len(combined) <= min_length:
                    current = combined
                else:
                    result.append(current)
                    current = sentence

    if current:
        result.append(current)

    return result


def speak(text, block=False, interrupt=True, clean=True):
    """Speak text using Kokoro TTS server.

    The TTS system supports:

    - Speed control via set_speed(0.5 to 2.0)
    - Volume control via set_volume(0.0 to 1.0)
    - Automatic chunking of long texts
    - Non-blocking operation with optional blocking mode
    - Interruption of current speech
    - Background processing of TTS requests

    Args:
        text: Text to speak
        block: If True, wait for audio to finish playing
        interrupt: If True, stop current speech and clear queue before speaking
        clean: If True, clean text for speech (remove markup, emojis, etc.)

    Example:
        >>> from gptme_tts.tts import speak, set_speed, set_volume
        >>> set_volume(0.8)  # Set comfortable volume
        >>> set_speed(1.2)   # Slightly faster speech
        >>> speak("Hello, world!")  # Non-blocking by default
        >>> speak("Important message!", interrupt=True)  # Interrupts previous speech
    """
    if clean:
        text = clean_for_speech(text).strip()

    log.info(f"Speaking text ({len(text)} chars)")

    # Stop current speech if requested
    if interrupt:
        stop()

    try:
        # Split text into chunks
        chunks = join_short_sentences(split_text(text))
        chunks = [c.replace("gptme", "gpt-me") for c in chunks]  # Fix pronunciation

        # Ensure TTS processor thread is running
        ensure_tts_thread()

        # Queue chunks for processing
        for chunk in chunks:
            if chunk.strip():
                tts_request_queue.put(chunk)

        if block:
            # Wait for all TTS processing to complete
            tts_request_queue.join()
            # Note: Audio playback blocking is now handled by the sound utility

    except Exception as e:
        log.error(f"Failed to queue text for speech: {e}")


# Hook functions for automatic TTS integration

# Matches a sentence end (.!?  not followed by another word char, so decimals
# like "3.14" are left intact) or a line break — both are natural speech flush
# points while streaming.
_re_speak_boundary = re.compile(r"[.!?](?=\s|$)|\n")


def _last_speak_boundary(text: str) -> int:
    """Return the index just past the last sentence/line boundary, or -1."""
    last = -1
    for match in _re_speak_boundary.finditer(text):
        last = match.end()
    return last


def _reset_stream_state() -> None:
    global _stream_raw, _stream_spoken_chars, _stream_first_segment
    _stream_raw = ""
    _stream_spoken_chars = 0
    _stream_first_segment = True


def reset_stream_on_generation(*args, **kwargs):
    """Hook: Reset streaming-speech state before each generation.

    Registered for GENERATION_PRE so a new response interrupts leftover speech
    and starts buffering from scratch.
    """
    _reset_stream_state()
    yield  # Hooks must be generators


def speak_on_chunk(chunk, **kwargs):
    """Hook: Speak complete sentences as the response streams in.

    Registered for GENERATION_CHUNK. Accumulates streamed text, cleans it (which
    strips tool blocks / markup, including blocks still being streamed), and
    voices each newly-completed sentence. The remainder is voiced at
    generation.post by speak_on_generation.
    """
    global _stream_raw, _stream_spoken_chars, _stream_first_segment
    _stream_raw += chunk
    cleaned = clean_for_speech(_stream_raw)
    boundary = _last_speak_boundary(cleaned)
    if boundary > _stream_spoken_chars:
        segment = cleaned[_stream_spoken_chars:boundary].strip()
        _stream_spoken_chars = boundary
        if segment:
            speak(segment, interrupt=_stream_first_segment, clean=False)
            _stream_first_segment = False
    yield  # Hooks must be generators


def speak_on_generation(message, workspace=None, **kwargs):
    """Hook: Speak assistant messages after generation.

    Registered for GENERATION_POST hook. When the response was streamed,
    speak_on_chunk has already voiced most of it, so only the trailing
    not-yet-spoken remainder is flushed here. For non-streamed responses
    (no chunks seen), the whole message is spoken.
    """
    # Only speak assistant messages
    if message.role != "assistant":
        return

    global _stream_raw, _stream_spoken_chars, _stream_first_segment
    if _stream_raw:
        # Streamed: flush whatever sentence tail wasn't voiced during streaming.
        cleaned = clean_for_speech(_stream_raw)
        if len(cleaned) > _stream_spoken_chars:
            tail = cleaned[_stream_spoken_chars:].strip()
            if tail:
                speak(tail, interrupt=_stream_first_segment, clean=False)
        _reset_stream_state()
    else:
        # Non-streamed fallback: speak the whole message.
        speak(message.content)
    yield  # Hooks must be generators


def wait_on_session_end(manager, **kwargs):
    """Hook: Wait for TTS to finish before session ends.

    Registered for SESSION_END hook.
    Replaces the old _wait_for_tts_if_enabled() function.
    """
    # Only wait if GPTME_VOICE_FINISH is enabled
    if os.environ.get("GPTME_VOICE_FINISH", "").lower() not in ["1", "true"]:
        return

    log.info("Waiting for TTS to finish...")
    try:
        # Wait for all TTS processing to complete
        tts_request_queue.join()
        log.info("TTS request queue joined")

        # Then wait for all audio to finish playing
        wait_for_audio()
        log.info("Audio playback finished")
    except KeyboardInterrupt:
        log.info("Interrupted while waiting for TTS")
        stop()

    yield  # Hooks must be generators


# Specific guidance shown when 'tts' is explicitly requested but unavailable.
# Guarded so it works on gptme builds that predate ToolSpec.available_hint.
_TTS_UNAVAILABLE_HINT = (
    "TTS is unavailable. For the local server backend, start tts_server.py "
    "(e.g. `uv run tts_server.py --backend kokoro`); for OpenRouter, set "
    "GPTME_TTS_BACKEND=openrouter and OPENROUTER_API_KEY."
)
_hint_kwargs = (
    {"available_hint": _TTS_UNAVAILABLE_HINT}
    if hasattr(ToolSpec, "available_hint")
    else {}
)

tool = ToolSpec(
    "tts",
    desc="Text-to-speech (TTS) tool for generating audio from text.",
    instructions="Automatically voices your responses to the user. Use speak() to add spoken emphasis or deliver important messages audibly. Note: you cannot hear the output, so do not rely on it for confirmation.",
    available=is_available,
    **_hint_kwargs,
    functions=[speak, set_speed, set_volume, stop],
    init=init,
    hooks={
        "reset_stream": (
            "generation.pre",
            reset_stream_on_generation,
            0,  # Normal priority
        ),
        "speak_on_chunk": (
            "generation.chunk",
            speak_on_chunk,
            0,  # Normal priority
        ),
        "speak_on_generation": (
            "generation.post",
            speak_on_generation,
            0,  # Normal priority
        ),
        "wait_on_session_end": (
            "session.end",
            wait_on_session_end,
            0,  # Normal priority
        ),
    },
)
