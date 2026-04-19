from gptme_voice.realtime.openai_client import SessionConfig
from gptme_voice.realtime.xai_client import XAIRealtimeClient


def test_xai_client_uses_xai_defaults() -> None:
    client = XAIRealtimeClient(api_key="test-key", session_config=SessionConfig())

    assert client.session_config.voice == "rex"
    assert client._get_ws_url() == "wss://api.x.ai/v1/realtime"
