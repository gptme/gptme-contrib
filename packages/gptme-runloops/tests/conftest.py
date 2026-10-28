"""Keep run-loop recording tests out of the operator's session ledger."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_session_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GPTME_SESSIONS_DIR", str(tmp_path / "sessions"))
