"""
Text-to-speech plugin for gptme.

Uses Kokoro for local TTS generation. Requires a running TTS server
(``tts_server.py`` included in this plugin).

Install with::

    pip install gptme-tts

Environment Variables:

- ``GPTME_TTS_VOICE``: Set the voice to use for TTS.
- ``GPTME_TTS_SPEED``: Set the speed (0.5-2.0, default 1.0).
- ``GPTME_VOICE_FINISH``: If "true" or "1", waits for speech to finish before exiting.
"""

__version__ = "0.1.0"

try:
    from .tts import tool  # noqa: F401
except ImportError:
    pass

from gptme.plugins import GptmePlugin

# Entry-point plugin manifest. Registered via the ``gptme.plugins`` group in
# pyproject.toml so an installed package is discovered without configuring
# plugin search paths. The tool module is imported lazily by gptme's tool
# discovery, so heavy/optional deps don't break plugin registration.
plugin = GptmePlugin(name="gptme-tts", tool_modules=["gptme_tts.tts"])
