"""Shared isolation for gptme-sessions tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_native_pi_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Never let all-harness CLI tests scan a developer's real Pi history."""
    empty_root = tmp_path_factory.getbasetemp() / "no-native-pi-sessions"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(empty_root))
