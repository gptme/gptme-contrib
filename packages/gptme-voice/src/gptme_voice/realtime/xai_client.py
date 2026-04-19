"""
xAI (Grok) Realtime API WebSocket client.

Drop-in replacement for OpenAIRealtimeClient using xAI's voice agent API.
The protocol is largely OpenAI-compatible; the differences are the endpoint
URL, authentication header, and default model name.

See: https://docs.x.ai/developers/model-capabilities/audio/voice-agent
"""

from gptme.config import get_config

from .openai_client import OpenAIRealtimeClient, SessionConfig

# Default OpenAI model sentinel — detected so we can swap in the Grok default
_OPENAI_DEFAULT_MODEL = "gpt-4o-realtime-preview-2024-12-17"
_DEFAULT_XAI_MODEL = "grok-2-realtime"


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
    - Defaults to the Grok realtime model
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
        if cfg.model == _OPENAI_DEFAULT_MODEL:
            import dataclasses

            cfg = dataclasses.replace(cfg, model=_DEFAULT_XAI_MODEL)

        super().__init__(api_key=resolved_key, session_config=cfg, **kwargs)

    def _get_ws_headers(self) -> dict[str, str]:
        """xAI auth — bearer token only, no OpenAI-Beta header."""
        return {"Authorization": f"Bearer {self.api_key}"}
