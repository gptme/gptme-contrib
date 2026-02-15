"""
Real-time voice interface for gptme using OpenAI Realtime API.

Provides WebSocket-based bidirectional audio streaming for low-latency
voice conversations, suitable for phone calls via Twilio Media Streams.

.. deprecated:: 0.1.0
    Use `gptme_voice.realtime` package instead.
    This module will be removed in a future version.
"""

import warnings

warnings.warn(
    "scripts.voice.realtime is deprecated. Use gptme_voice.realtime instead.",
    DeprecationWarning,
    stacklevel=2,
)

__version__ = "0.1.0"
