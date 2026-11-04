"""Explicit runner-environment contract (ErikBjare/bob#1409).

Pure builder plus the ``run_work_file`` integration seam. No credentials,
network or live services: values are synthetic and every assertion is about
*selection* (which names cross the boundary), never about secret content.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest
from gptme_runloops.run_item import run_work_file
from gptme_runloops.runner_env import (
    DEFAULT_CREDENTIALS,
    DOTENV_DISABLE_VAR,
    MissingRunnerEnv,
    build_runner_base_env,
    runner_env_from_spec,
)

PARENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/agent",
    "LANG": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "OPENROUTER_API_KEY": "synthetic-inference",
    "XAI_API_KEY": "synthetic-xai",
    "PM_UNRELATED_SECRET": "synthetic-unrelated",
    "AWS_SECRET_ACCESS_KEY": "synthetic-storage",
    "TWITTER_API_SECRET": "synthetic-social",
}


# --- pure builder ---


def test_default_contract_selects_settings_not_credentials() -> None:
    env = build_runner_base_env(PARENT, "claude-code")
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/agent"
    assert env["LANG"] == "C.UTF-8"
    assert env["LC_CTYPE"] == "C.UTF-8"  # prefix selection
    assert env[DOTENV_DISABLE_VAR] == "1"
    # File-auth backends need no ambient credential and unrelated authority is
    # never selected.
    assert "OPENROUTER_API_KEY" not in env
    assert not any(
        name.startswith(("PM_UNRELATED", "AWS_", "TWITTER_")) for name in env
    )


def test_gptme_keeps_only_its_own_inference_credential() -> None:
    env = build_runner_base_env(PARENT, "gptme")
    assert env["OPENROUTER_API_KEY"] == "synthetic-inference"
    assert "XAI_API_KEY" not in env  # a different backend's credential
    assert "PM_UNRELATED_SECRET" not in env


def test_explicit_credential_list_overrides_backend_default() -> None:
    assert "OPENROUTER_API_KEY" not in build_runner_base_env(
        PARENT, "gptme", credentials=[]
    )
    env = build_runner_base_env(PARENT, "claude-code", credentials=["XAI_API_KEY"])
    assert env["XAI_API_KEY"] == "synthetic-xai"
    assert DEFAULT_CREDENTIALS.get("claude-code", ()) == ()


def test_missing_required_setting_fails_closed_without_values() -> None:
    parent = {"OPENROUTER_API_KEY": "synthetic-inference"}
    with pytest.raises(MissingRunnerEnv) as excinfo:
        build_runner_base_env(parent, "gptme")
    message = str(excinfo.value)
    assert "HOME" in message and "PATH" in message
    assert "synthetic-inference" not in message


def test_extra_values_win_and_input_is_not_mutated() -> None:
    before = dict(PARENT)
    env = build_runner_base_env(
        PARENT,
        "gptme",
        extra={"PYTHON_DOTENV_DISABLED": "0", "BOB_BACKEND": "gptme"},
    )
    assert env["PYTHON_DOTENV_DISABLED"] == "0"
    assert env["BOB_BACKEND"] == "gptme"
    assert PARENT == before
    assert DOTENV_DISABLE_VAR not in PARENT


def test_dotenv_control_can_be_disabled_explicitly() -> None:
    assert DOTENV_DISABLE_VAR not in build_runner_base_env(
        PARENT, "gptme", disable_dotenv=False
    )


# --- config-table resolution ---


def test_spec_modes_are_explicit() -> None:
    assert runner_env_from_spec({"mode": "legacy"}, PARENT, backend="gptme") is None
    explicit = runner_env_from_spec({"mode": "explicit"}, PARENT, backend="gptme")
    assert (
        explicit is not None and explicit["OPENROUTER_API_KEY"] == "synthetic-inference"
    )


@pytest.mark.parametrize("spec", [{}, {"mode": "ambient"}, {"mode": True}])
def test_unknown_or_missing_mode_is_an_error_not_a_fallback(spec) -> None:
    with pytest.raises(ValueError, match="runner_env.mode"):
        runner_env_from_spec(spec, PARENT, backend="gptme")


def test_spec_rejects_malformed_lists_and_tables() -> None:
    with pytest.raises(ValueError, match="passthrough"):
        runner_env_from_spec(
            {"mode": "explicit", "passthrough": "PATH"}, PARENT, backend="gptme"
        )
    with pytest.raises(ValueError, match="set"):
        runner_env_from_spec(
            {"mode": "explicit", "set": ["not", "a", "table"]},
            PARENT,
            backend="gptme",
        )


def test_spec_passthrough_extensions_are_honored() -> None:
    parent = dict(PARENT)
    parent["BOB_GRAPHQL_ATTRIBUTION_WRAPPER"] = "1"
    env = runner_env_from_spec(
        {
            "mode": "explicit",
            "passthrough": ["PATH", "HOME", "BOB_GRAPHQL_ATTRIBUTION_WRAPPER"],
            "prefixes": [],
            "credentials": ["OPENROUTER_API_KEY"],
        },
        parent,
        backend="gptme",
    )
    assert env is not None
    assert env["BOB_GRAPHQL_ATTRIBUTION_WRAPPER"] == "1"
    assert "LC_CTYPE" not in env  # prefixes explicitly disabled
    assert "PM_UNRELATED_SECRET" not in env


# --- run_work_file integration ---


class _RecordingRunCmd:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        import subprocess

        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, "", "")

    def runner_envs(self) -> list[dict]:
        return [c["env"] for c in self.calls if "/fake/run.sh" in c["argv"]]


_HELPERS: ModuleType | None = None


def _helpers() -> ModuleType:
    """Load the sibling test module's fixtures without a test package."""
    global _HELPERS
    if _HELPERS is None:
        path = Path(__file__).with_name("test_run_item.py")
        spec = importlib.util.spec_from_file_location("_test_run_item_helpers", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # Dataclass field resolution looks the defining module up in
        # sys.modules, so register it before executing.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _HELPERS = module
    return _HELPERS


def _config(tmp_path: Path, **kwargs):
    return _helpers().make_config(tmp_path, **kwargs)


def _hooks(run_cmd):
    return _helpers().make_hooks(run_cmd=run_cmd)


def _work_file(tmp_path: Path) -> Path:
    item = _helpers().make_item()
    path = tmp_path / "slot.jsonl"
    path.write_text(item.raw_line + "\n")
    return path


def test_run_work_file_explicit_contract_excludes_unrelated_secret(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-inference")
    monkeypatch.setenv("PM_UNRELATED_SECRET", "synthetic-unrelated")
    run_cmd = _RecordingRunCmd()
    config = _config(
        tmp_path,
        ambient_harness_env="BOB_AMBIENT_HARNESS",
        runner_env={
            "mode": "explicit",
            "set": {"BOB_GRAPHQL_ATTRIBUTION_WRAPPER": "1"},
        },
    )
    rc = run_work_file(
        _work_file(tmp_path),
        config,
        _hooks(run_cmd),
        backend="gptme",
        model="gpt-6.1-sol",
        lane="slow",
        dispatch_id="unit-explicit",
    )
    assert rc == 0
    envs = run_cmd.runner_envs()
    assert len(envs) == 1
    env = envs[0]
    assert env["OPENROUTER_API_KEY"] == "synthetic-inference"
    assert env["BOB_GRAPHQL_ATTRIBUTION_WRAPPER"] == "1"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert "PM_UNRELATED_SECRET" not in env
    # Plan metadata and the ambient-harness tag still apply.
    assert env["BOB_SESSION_ID"] and env["AGENT_SESSION_ID"]
    assert env["AGENT_AMBIENT_HARNESS"].startswith("gptme:")


def test_run_work_file_legacy_mode_keeps_inheritance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_UNRELATED_SECRET", "synthetic-unrelated")
    run_cmd = _RecordingRunCmd()
    config = _config(tmp_path, runner_env={"mode": "legacy"})
    rc = run_work_file(
        _work_file(tmp_path),
        config,
        _hooks(run_cmd),
        backend="gptme",
        lane="slow",
        dispatch_id="unit-legacy",
    )
    assert rc == 0
    env = run_cmd.runner_envs()[0]
    assert env["PM_UNRELATED_SECRET"] == "synthetic-unrelated"


def test_run_work_file_misdeclared_contract_fails_before_launch(
    tmp_path, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="gptme_runloops.run_item")
    monkeypatch.setenv("PM_UNRELATED_SECRET", "synthetic-unrelated")
    run_cmd = _RecordingRunCmd()
    config = _config(tmp_path, runner_env={"mode": "ambient"})
    rc = run_work_file(
        _work_file(tmp_path),
        config,
        _hooks(run_cmd),
        backend="gptme",
        lane="slow",
        dispatch_id="unit-bad-mode",
    )
    assert rc == 78
    assert run_cmd.runner_envs() == []  # nothing launched
    assert "runner_env.mode" in caplog.text


def test_run_work_file_missing_required_setting_names_only(
    tmp_path, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="gptme_runloops.run_item")
    monkeypatch.delenv("HOME", raising=False)
    run_cmd = _RecordingRunCmd()
    config = _config(tmp_path, runner_env={"mode": "explicit"})
    rc = run_work_file(
        _work_file(tmp_path),
        config,
        _hooks(run_cmd),
        backend="gptme",
        lane="slow",
        dispatch_id="unit-missing-home",
    )
    assert rc == 78
    assert run_cmd.runner_envs() == []
    assert "HOME" in caplog.text
