"""Shared isolation for gptme-sessions tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_native_pi_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Never let all-harness CLI tests scan a developer's real Pi history."""
    empty_root = tmp_path_factory.getbasetemp() / "no-native-pi-sessions"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(empty_root))


@pytest.fixture(autouse=True)
def isolate_live_openrouter_config(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep pytest off operator gptme config.local.toml OpenRouter keys.

    ``_resolve_openrouter_api_key`` / ``_load_config_env`` bind
    ``config_paths=CONFIG_PATHS`` at import. Patching the module name is a
    no-op for those defaults. Trajectory-secret-exposure 2026-11-02 captured
    pytest assertion diffs of live OPENROUTER_API_KEY_JUDGE. Opt out with
    ``@pytest.mark.allow_live_openrouter_config``.
    """
    if request.node.get_closest_marker("allow_live_openrouter_config"):
        return
    for name in list(os.environ):
        if "OPENROUTER_API_KEY" in name:
            monkeypatch.delenv(name, raising=False)
    try:
        from gptme_sessions import judge
    except Exception:
        return
    live_paths = judge.CONFIG_PATHS
    real_load = judge._load_config_env

    def load_config_env(config_paths=live_paths):
        if config_paths is live_paths:
            return {}
        return real_load(config_paths)

    monkeypatch.setattr(judge, "_load_config_env", load_config_env)
    monkeypatch.setattr(judge, "CONFIG_PATHS", ())
