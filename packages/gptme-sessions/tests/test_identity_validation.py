"""Reject non-text recording identities before inference or serialization."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore


@pytest.mark.parametrize("field", ["harness", "model"])
@pytest.mark.parametrize("value", [MagicMock(), 123, ["gptme"]])
def test_post_session_rejects_non_string_identity(tmp_path: Path, field, value):
    store = SessionStore(sessions_dir=tmp_path)
    kwargs = {"harness": "gptme", "model": "test-model", field: value}
    with pytest.raises(TypeError, match=f"{field} must be a str"):
        post_session(store=store, **kwargs)
    assert not store.path.exists()


def test_model_none_remains_supported(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(store=store, harness="gptme", model=None)
    assert result.record.model == "unknown"
