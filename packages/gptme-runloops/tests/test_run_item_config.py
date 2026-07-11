"""Tests for run-item config loading + default hook assembly (CLI side)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from gptme_runloops.cli import main as cli_main
from gptme_runloops.run_item_config import (
    assemble_hooks,
    default_config_path,
    load_run_item_config,
)


def test_missing_config_file_yields_defaults(tmp_path) -> None:
    config, raw = load_run_item_config(tmp_path)
    assert raw == {}
    assert config.workspace == tmp_path.resolve()
    assert config.agent_name == "Agent"
    assert config.default_timeout == 900
    assert config.resolved_dispatch_ledger == (
        tmp_path.resolve() / "state/project-monitoring-dispatch.jsonl"
    )


def test_toml_config_overrides_and_path_resolution(tmp_path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "pm-run-item.toml").write_text(
        """
[config]
author = "TimeToBuildBob"
agent_name = "Bob"
operator_name = "Erik"
primary_repo = "ErikBjare/bob"
greptile_repos_pattern = "^gptme/gptme$"
self_merge_repos = "ErikBjare/bob"
dispatch_ledger = "state/custom-ledger.jsonl"
monitoring_rules_file = "/abs/rules.md"
default_timeout = 600
unknown_field_is_ignored = true

[hooks]
post_run = ["scripts/summary.sh", "--flag"]
delivery_check = []
"""
    )
    config, raw = load_run_item_config(tmp_path)
    assert config.author == "TimeToBuildBob"
    assert config.agent_name == "Bob"
    assert config.default_timeout == 600
    # Relative paths resolve against the workspace; absolute stay put
    assert (
        config.resolved_dispatch_ledger
        == tmp_path.resolve() / "state/custom-ledger.jsonl"
    )
    assert config.resolved_monitoring_rules_file == Path("/abs/rules.md")
    assert default_config_path(tmp_path) == tmp_path / "config" / "pm-run-item.toml"

    hooks = assemble_hooks(config, raw)
    # TOML hook with a relative first element that doesn't exist stays as-is
    assert hooks.post_run == ["scripts/summary.sh", "--flag"]
    # Empty list disables a hook
    assert hooks.delivery_check is None


def test_env_overrides_win_over_toml(tmp_path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "pm-run-item.toml").write_text(
        '[config]\nself_merge_repos = "from/toml"\n'
    )
    monkeypatch.setenv("PROJECT_MONITORING_SELF_MERGE_REPOS", "from/env")
    config, _ = load_run_item_config(tmp_path)
    assert config.self_merge_repos == "from/env"


def test_cli_overrides_win_over_everything(tmp_path) -> None:
    config, _ = load_run_item_config(tmp_path, author="CliAuthor", agent_name=None)
    assert config.author == "CliAuthor"
    assert config.agent_name == "Agent"  # None override ignored


def test_assemble_hooks_degrades_when_scripts_missing(tmp_path) -> None:
    config, raw = load_run_item_config(tmp_path)
    hooks = assemble_hooks(config, raw)
    # No conventional scripts in an empty workspace → hooks degrade to None
    assert hooks.sysprompt_builder is None
    assert hooks.delivery_check is None
    assert hooks.wait_merge_gate is None
    assert hooks.arc_manager is None
    assert hooks.assigned_issue_ack is None
    assert hooks.merge_lifecycle_io is None
    assert hooks.self_merge_gate_available is False
    assert hooks.greptile_helper_available is False
    # The runner default is always the conventional path (run.sh)
    assert hooks.runner == [str(tmp_path.resolve() / "run.sh")]


def test_assemble_hooks_picks_up_conventional_scripts(tmp_path) -> None:
    ws = tmp_path
    for rel in (
        "scripts/build-system-prompt.sh",
        "scripts/runs/github/check-pm-delivery.py",
        "scripts/github/should-auto-wait-and-merge.py",
        "scripts/github/pr-address-wait-and-merge.sh",
        "scripts/github/self-merge-check.py",
        "scripts/github/self-merge-if-eligible.sh",
        "scripts/github/greptile-helper.sh",
        "scripts/tasks/arc_manager.py",
        "scripts/project_monitoring_assigned_issue_ack.py",
        "scripts/session-records.py",
    ):
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    config, raw = load_run_item_config(ws, self_merge_repos="o/r")
    hooks = assemble_hooks(config, raw)
    assert hooks.sysprompt_builder is not None
    assert hooks.sysprompt_builder[1:] == ["--context-args", "--type monitoring"]
    assert hooks.delivery_check is not None
    assert hooks.merge_lifecycle_io is not None
    assert hooks.merge_lifecycle_io.env["WORKSPACE_REPO"] == "o/r"
    assert hooks.self_merge_gate_available is True
    assert hooks.greptile_helper_available is True


def test_cli_dry_run_prints_execution_plan(tmp_path) -> None:
    work_file = tmp_path / "slot.jsonl"
    work_file.write_text(
        json.dumps(
            {
                "repo": "gptme/gptme-contrib",
                "number": 1234,
                "title": "a PR",
                "types": ["pr_update"],
                "type": "pr_update",
                "detail": "review comment",
                "all_numbers": [1234],
            }
        )
        + "\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "run-item",
            "--workspace",
            str(tmp_path),
            "--work-file",
            str(work_file),
            "--backend",
            "claude-code",
            "--model",
            "claude-sonnet-4-6",
            "--lane",
            "slow",
            "--slot-key",
            "gptme/gptme-contrib#1234",
            "--agent-name",
            "Bob",
            "--author",
            "TimeToBuildBob",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # Diagnostics go to stderr; stdout is pure plan JSON. CliRunner may mix
    # streams depending on click version — parse from the first brace.
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["backend"] == "claude-code"
    assert payload["claim_mode"] == "acquire"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["claim_key"] == "github:gptme/gptme-contrib#1234"
    assert item["timeout"] == 900
    assert "You are Bob," in item["prompt"]
    assert "Your GitHub author name is: TimeToBuildBob" in item["prompt"]


def test_cli_requires_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BOB_BACKEND", raising=False)
    work_file = tmp_path / "slot.jsonl"
    work_file.write_text("{}\n")
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["run-item", "--workspace", str(tmp_path), "--work-file", str(work_file)],
    )
    assert result.exit_code != 0
    assert "--backend is required" in result.output
