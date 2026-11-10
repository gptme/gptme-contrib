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


# Spawner-provided session lineage env that ``post_session`` falls back to.
# An agent running this suite from inside its own session exports these, and
# they override whatever a test sets via the legacy ``BOB_*`` alias.
_DISPATCH_ENV = (
    "AGENT_DISPATCH_CAUSE",
    "BOB_DISPATCH_CAUSE",
    "AGENT_DISPATCH_KIND",
    "BOB_DISPATCH_KIND",
    "AGENT_DISPATCH_ID",
    "BOB_DISPATCH_ID",
    "PM_DISPATCH_ID",
    "AGENT_PARENT_SESSION_ID",
    "BOB_PARENT_SESSION_ID",
    "GIT_COMMITTER_SESSION_ID",
)


@pytest.fixture(autouse=True)
def isolate_host_dispatch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no inherited dispatch/lineage env.

    Without this, running the suite inside an agent session (which exports
    ``AGENT_DISPATCH_CAUSE`` etc.) fails the dispatch-cause tests, and the
    failures read as "pre-existing" noise that hides real regressions.
    """
    for name in _DISPATCH_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_host_git_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's global git hooks and signing off throwaway test repos.

    Agent hosts set ``core.hooksPath`` to hooks that enforce an identity
    allowlist, so ``git commit`` as ``test@example.com`` in a tmp repo aborts.
    Env-injected config outranks global and repo config for every git
    subprocess the tests spawn.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
