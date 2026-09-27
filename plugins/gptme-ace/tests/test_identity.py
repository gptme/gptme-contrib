"""Tests for workspace/agent-identity resolution (#1705).

A non-Bob agent must resolve its own workspace and name instead of inheriting
Bob's hard-coded ``Path.home() / "bob"`` storage root and "Bob" prompt identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_ace.generator import domain_context
from gptme_ace.identity import (
    DEFAULT_AGENT_NAME,
    agent_name,
    default_workspace_root,
    detect_agent_repo,
    workspace_from_env,
)
from gptme_ace.storage import InsightStorage


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """Isolate every test from ambient identity env vars."""
    for var in ("BOB_WORKSPACE", "AGENT_WORKSPACE", "BOB_BRAIN_ROOT", "AGENT_NAME"):
        monkeypatch.delenv(var, raising=False)


def _make_agent_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gptme.toml").write_text('[agent]\nname = "YourAgent"\n')
    (root / "gptme-contrib").mkdir(exist_ok=True)
    return root


# --- workspace_from_env ----------------------------------------------------


def test_workspace_from_env_unset_returns_none():
    assert workspace_from_env({}) is None


def test_workspace_from_env_prefers_legacy_bob_var():
    env = {"BOB_WORKSPACE": "/home/bob/bob", "AGENT_WORKSPACE": "/home/other/other"}
    assert workspace_from_env(env) == Path("/home/bob/bob")


def test_workspace_from_env_accepts_neutral_and_brain_root():
    assert workspace_from_env({"AGENT_WORKSPACE": "/home/other/other"}) == Path(
        "/home/other/other"
    )
    assert workspace_from_env({"BOB_BRAIN_ROOT": "/srv/agent"}) == Path("/srv/agent")


def test_workspace_from_env_ignores_relative_values():
    # A relative value would resolve against cwd and could land insight storage
    # inside a checkout subdirectory.
    assert workspace_from_env({"AGENT_WORKSPACE": "relative/path"}) is None
    assert workspace_from_env({"AGENT_WORKSPACE": "~/agent"}) is None


# --- detect_agent_repo -----------------------------------------------------


def test_detect_agent_repo_from_nested_dir(tmp_path):
    root = _make_agent_repo(tmp_path / "agent")
    nested = root / "gptme-contrib" / "plugins" / "gptme-ace" / "src"
    nested.mkdir(parents=True)
    assert detect_agent_repo(nested) == root


def test_detect_agent_repo_requires_both_markers(tmp_path):
    # gptme.toml alone is not an agent repo (plain gptme project).
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "gptme.toml").write_text("[agent]\nname = 'X'\n")
    assert detect_agent_repo(plain) is None


# --- default_workspace_root ------------------------------------------------


def test_default_workspace_root_env_wins(tmp_path):
    env = {"AGENT_WORKSPACE": str(tmp_path)}
    assert default_workspace_root(environ=env, start=tmp_path) == tmp_path


def test_default_workspace_root_detects_repo(tmp_path):
    root = _make_agent_repo(tmp_path / "agent")
    assert default_workspace_root(environ={}, start=root / "gptme-contrib") == root


def test_default_workspace_root_home_fallback(tmp_path, monkeypatch):
    # No env, no detectable agent repo: fall back to $HOME/<home-name> when it
    # exists (the generic form of the old Path.home() / "bob" default).
    home = tmp_path / "agent"
    workspace = home / "agent"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert default_workspace_root(environ={}, start=tmp_path / "elsewhere") == workspace


def test_default_workspace_root_home_fallback_without_workspace_dir(
    tmp_path, monkeypatch
):
    home = tmp_path / "agent"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert default_workspace_root(environ={}, start=tmp_path / "elsewhere") == home


# --- agent_name ------------------------------------------------------------


def test_agent_name_env_wins(tmp_path, monkeypatch):
    _make_agent_repo(tmp_path)
    monkeypatch.setenv("AGENT_NAME", "FromEnv")
    assert agent_name(tmp_path) == "FromEnv"


def test_agent_name_reads_workspace_config(tmp_path):
    _make_agent_repo(tmp_path)
    assert agent_name(tmp_path) == "YourAgent"


def test_agent_name_defaults_to_bob(tmp_path):
    assert agent_name(tmp_path / "no-config-here") == DEFAULT_AGENT_NAME


# --- domain_context --------------------------------------------------------


def test_domain_context_uses_workspace_identity(tmp_path):
    root = _make_agent_repo(tmp_path / "other-agent")
    context = domain_context(root)
    assert "- Agent: YourAgent (autonomous AI assistant)" in context
    assert "other-agent (workspace)" in context
    assert "Bob" not in context


def test_domain_context_keeps_bob_text_for_bob(tmp_path):
    root = _make_agent_repo(tmp_path / "bob")
    # Bob's gptme.toml sets [agent].name = "Bob"
    (root / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    context = domain_context(root)
    assert "- Agent: Bob (autonomous AI assistant)" in context
    assert "bob (workspace)" in context


def test_domain_context_explicit_agent_name(tmp_path):
    context = domain_context(tmp_path, agent_name="Sven")
    assert "- Agent: Sven (autonomous AI assistant)" in context


def test_domain_context_without_workspace_resolves_repo_from_identity(
    tmp_path, monkeypatch
):
    """A no-arg ``domain_context()`` must not fall back to Bob's repo name."""
    workspace = tmp_path / "sven"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("AGENT_NAME", "Sven")
    context = domain_context()
    assert "- Agent: Sven (autonomous AI assistant)" in context
    assert "sven (workspace)" in context
    assert "bob (workspace)" not in context


# --- storage root ----------------------------------------------------------


def test_insight_storage_uses_env_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    storage = InsightStorage()
    assert storage.workspace_root == tmp_path
    assert storage.raw_dir == tmp_path / "insights" / "raw"
    assert storage.raw_dir.is_dir()


def test_insight_storage_explicit_root_still_wins(tmp_path):
    explicit = tmp_path / "explicit"
    storage = InsightStorage(explicit)
    assert storage.workspace_root == explicit
    assert storage.raw_dir.is_dir()
