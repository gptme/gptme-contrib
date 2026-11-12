"""Credential lookup must never send a token to a host from another source."""

import os
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/home-assistant/scripts/ha.py"


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in ("HA_HOST", "HA_API_KEY", "BOB_SECRETS_DIR"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    # Bound the real CWD-to-root lookup without mocking parent-directory discovery.
    (tmp_path / ".env").write_text("# Test boundary: no HA credentials here\n")
    secrets = home / ".config/secrets"
    secrets.mkdir(parents=True)
    return workspace, secrets


@pytest.mark.parametrize(
    "workspace_config,private_config",
    [
        ("HA_HOST=https://attacker.example\n", "HA_API_KEY=private-token\n"),
        (
            "HA_HOST=https://attacker.example\n",
            "HA_HOST=https://private.example\nHA_API_KEY=private-token\n",
        ),
        ("HA_API_KEY=workspace-token\n", "HA_HOST=https://private.example\n"),
    ],
)
def test_partial_workspace_credentials_do_not_borrow_from_private_file(
    config, monkeypatch, workspace_config, private_config
):
    workspace, secrets = config
    (workspace / ".env").write_text(workspace_config)
    (secrets / "ha.env").write_text(private_config)
    module = runpy.run_path(str(SCRIPT))
    request = module["ha_request"]
    urlopen = Mock()
    monkeypatch.setitem(request.__globals__, "urlopen", urlopen)

    with pytest.raises(SystemExit) as exc:
        request("")

    assert exc.value.code == 1
    urlopen.assert_not_called()


@pytest.mark.parametrize(
    "name,value",
    [
        ("HA_HOST", "https://process.example"),
        ("HA_API_KEY", "process-token"),
        ("HA_HOST", ""),
    ],
)
def test_partial_process_configuration_fails_closed(config, monkeypatch, name, value):
    workspace, secrets = config
    monkeypatch.setenv(name, value)
    complete = "HA_HOST=https://file.example\nHA_API_KEY=file-token\n"
    (workspace / ".env").write_text(complete)
    (secrets / "ha.env").write_text(complete)
    module = runpy.run_path(str(SCRIPT))
    request = module["ha_request"]
    urlopen = Mock()
    monkeypatch.setitem(request.__globals__, "urlopen", urlopen)
    with pytest.raises(SystemExit):
        request("")
    urlopen.assert_not_called()


@pytest.mark.parametrize("source", ["process", "workspace", "private", "override"])
def test_complete_credentials_preserve_source_precedence(
    config, monkeypatch, tmp_path, source
):
    workspace, secrets = config
    (secrets / "ha.env").write_text(
        "HA_HOST=https://private.example/\nHA_API_KEY=private-token\n"
    )
    if source in ("process", "workspace"):
        (workspace / ".env").write_text(
            "HA_HOST=https://workspace.example/\nHA_API_KEY=workspace-token\n"
        )
        nested = workspace / "nested"
        nested.mkdir()
        monkeypatch.chdir(nested)
    else:
        (workspace / ".env").write_text("# Unrelated workspace settings\nOTHER=value\n")
    if source == "process":
        monkeypatch.setenv("HA_HOST", "https://process.example/")
        monkeypatch.setenv("HA_API_KEY", "process-token")
    elif source == "override":
        override = tmp_path / "override"
        override.mkdir()
        (override / "ha.env").write_text(
            "HA_HOST='https://override.example/'\nHA_API_KEY=\"override-token\"\n"
        )
        monkeypatch.setenv("BOB_SECRETS_DIR", str(override))
    before = dict(os.environ)
    module = runpy.run_path(str(SCRIPT))
    assert module["HA_HOST"] == f"https://{source}.example"
    assert module["HA_API_KEY"] == f"{source}-token"
    assert dict(os.environ) == before


@pytest.mark.parametrize("source", ["workspace", "private"])
@pytest.mark.parametrize("first_token", ["valid-token", ""])
def test_duplicate_file_keys_keep_first_declaration(config, source, first_token):
    workspace, secrets = config
    path = workspace / ".env" if source == "workspace" else secrets / "ha.env"
    path.write_text(
        "HA_HOST=https://first.example/\n"
        "HA_HOST=https://second.example/\n"
        f"HA_API_KEY={first_token}\n"
        "HA_API_KEY=expired-token\n"
    )
    module = runpy.run_path(str(SCRIPT))
    assert module["HA_HOST"] == "https://first.example"
    assert module["HA_API_KEY"] == first_token


def test_missing_credentials_make_no_request(config, monkeypatch, capsys):
    module = runpy.run_path(str(SCRIPT))
    request = module["ha_request"]
    urlopen = Mock()
    monkeypatch.setitem(request.__globals__, "urlopen", urlopen)
    with pytest.raises(SystemExit):
        request("")
    urlopen.assert_not_called()
    assert "same source" in capsys.readouterr().err
