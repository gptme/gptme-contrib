"""Empty OPENAI_BASE_URL / VISION_MODEL are not treated as unset (OPENAI_API_KEY is)."""

import numpy as np
from gptme_vision_node import look


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "a desk"}}]}

    def close(self):
        pass


def test_empty_base_url_env_falls_back_to_default(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        return _Resp()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setattr(look.httpx, "post", fake_post)
    look.describe_frame(np.zeros((32, 32, 3), np.uint8))
    assert seen["url"] == f"{look.DEFAULT_BASE_URL}/chat/completions"


def test_empty_vision_model_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "")
    body = look.build_request(np.zeros((32, 32, 3), np.uint8))
    assert body["model"] == look.DEFAULT_MODEL
