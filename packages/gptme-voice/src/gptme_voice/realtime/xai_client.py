"""
xAI (Grok) Realtime API WebSocket client.

Drop-in replacement for OpenAIRealtimeClient using xAI's voice agent API.
The protocol is largely OpenAI-compatible; the differences are the endpoint
URL, authentication header, and xAI-specific defaults.

See: https://docs.x.ai/developers/model-capabilities/audio/voice-agent
"""

import dataclasses
import logging

from gptme.config import get_config

from .openai_client import OpenAIRealtimeClient, SessionConfig

logger = logging.getLogger(__name__)

_OPENAI_DEFAULT_VOICE = "echo"
# Derived from SessionConfig field default — stays in sync without duplication
_OPENAI_DEFAULT_MODEL: str = next(
    f.default  # type: ignore[assignment]
    for f in dataclasses.fields(SessionConfig)
    if f.name == "model"
)
# "rex" = male, confident, clear — matches the Bob persona better than "eve" (female)
_DEFAULT_XAI_VOICE = "rex"
_DEFAULT_XAI_MODEL = "grok-voice-think-fast-1.0"


def _get_xai_api_key() -> str | None:
    """Get xAI API key from gptme config (env var, project, or user config)."""
    return get_config().get_env("XAI_API_KEY")


class XAIRealtimeClient(OpenAIRealtimeClient):
    """
    WebSocket client for xAI Grok Realtime API.

    Identical to OpenAIRealtimeClient except:
    - Connects to api.x.ai instead of api.openai.com
    - Uses XAI_API_KEY for authentication
    - No OpenAI-Beta header
    - Defaults to an xAI-supported voice
    """

    WS_URL = "wss://api.x.ai/v1/realtime"

    def __init__(
        self,
        api_key: str | None = None,
        session_config: SessionConfig | None = None,
        **kwargs,
    ):
        resolved_key = api_key or _get_xai_api_key()
        if not resolved_key:
            raise ValueError(
                "XAI_API_KEY not found. Set it in gptme config or as an env var."
            )

        cfg = session_config or SessionConfig()
        if cfg.voice == _OPENAI_DEFAULT_VOICE:
            cfg = dataclasses.replace(cfg, voice=_DEFAULT_XAI_VOICE)
        if cfg.model == _OPENAI_DEFAULT_MODEL:
            cfg = dataclasses.replace(cfg, model=_DEFAULT_XAI_MODEL)

        # VAD tuning for Grok interruption (from task #651 / Erik feedback)
        # Lower threshold = more sensitive to speech, easier to interrupt
        # Keep end-of-turn silence conservative so noisy lines do not chop up speech
        # Prefix padding reduced to minimize lag
        if cfg.vad_threshold >= 0.65:  # only override default/high values
            cfg = dataclasses.replace(
                cfg,
                vad_threshold=0.55,
                vad_silence_duration_ms=500,
                vad_prefix_padding_ms=150,
            )

        super().__init__(api_key=resolved_key, session_config=cfg, **kwargs)

    def _get_ws_url(self) -> str:
        """xAI supports ?model= in the WebSocket URL."""
        return f"{self.WS_URL}?model={self.session_config.model}"

    def _get_ws_headers(self) -> dict[str, str]:
        """xAI auth — bearer token only, no OpenAI-Beta header."""
        return {"Authorization": f"Bearer {self.api_key}"}

    def _get_transcription_config(self) -> dict | None:
        """xAI does not support whisper-1; omit transcription config."""
        return None

    # xAI does not emit session.created; it emits session.updated instead.
    # The base class already handles session.updated via _mark_session_ready,
    # so no override is needed here.
