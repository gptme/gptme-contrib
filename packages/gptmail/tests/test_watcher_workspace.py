"""Tests for the email watcher's workspace-root resolution.

Covers the ``GPTME_WORKSPACE`` override added so installed/symlinked layouts
don't silently resolve to the wrong directory via the source-tree relative
fallback.
"""

from pathlib import Path

from gptmail.watcher import _resolve_workspace_dir


def test_env_override_takes_precedence(tmp_path: Path) -> None:
    ws = tmp_path / "my-workspace"
    ws.mkdir()
    resolved = _resolve_workspace_dir(env={"GPTME_WORKSPACE": str(ws)})
    assert resolved == ws.resolve()


def test_env_override_expands_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = _resolve_workspace_dir(env={"GPTME_WORKSPACE": "~/ws"})
    assert resolved == (tmp_path / "ws").resolve()


def test_env_override_expands_user_from_env_home(tmp_path: Path) -> None:
    # HOME in the env dict is used — no os.environ patch needed.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    resolved = _resolve_workspace_dir(env={"GPTME_WORKSPACE": "~/ws", "HOME": str(home_dir)})
    assert resolved == (home_dir / "ws").resolve()


def test_blank_override_falls_back_to_script_dir() -> None:
    # Mirrors the real layout: <ws>/packages/gptmail/src/gptmail -> <ws>
    fake_script_dir = Path("/opt/ws/packages/gptmail/src/gptmail")
    resolved = _resolve_workspace_dir(env={"GPTME_WORKSPACE": "  "}, script_dir=fake_script_dir)
    assert resolved == Path("/opt/ws")


def test_missing_override_uses_relative_fallback() -> None:
    fake_script_dir = Path("/home/agent/packages/gptmail/src/gptmail")
    resolved = _resolve_workspace_dir(env={}, script_dir=fake_script_dir)
    assert resolved == Path("/home/agent")
