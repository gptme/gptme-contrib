"""Tests for the PM one-item plan and subprocess boundary."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from gptme_runloops.cli import main
from gptme_runloops.run_item import (
    ASSIGNED_ISSUE_TIMEOUT,
    DEFAULT_TIMEOUT,
    GREPTILE_TIMEOUT,
    RunItem,
    RunItemConfig,
    RunItemHooks,
    execute_plan,
    load_items,
    plan_run_item,
)


def item(**overrides: object) -> RunItem:
    data: dict[str, object] = {
        "repo": "gptme/gptme",
        "number": 42,
        "title": "A title",
        "detail": "normal",
        "types": ["pr_update"],
        "all_numbers": [42],
        "extra_gate_field": {"kept": True},
    }
    data.update(overrides)
    return RunItem.from_grouped_json(json.dumps(data))


def config(tmp_path: Path, **overrides: object) -> RunItemConfig:
    data: dict[str, object] = {
        "workspace": tmp_path,
        "backend": "codex",
        "model": "gpt-5.6-terra",
        "run_salt": "1700000000",
        "author": "TimeToBuildBob",
        "agent_name": "Bob",
    }
    data.update(overrides)
    return RunItemConfig(**data)  # type: ignore[arg-type]


def test_parse_grouped_item_preserves_unknown_fields() -> None:
    parsed = item(number="master-ci", types=["master_ci_failure"], all_numbers=[11, 12])
    assert parsed.number == "master-ci"
    assert parsed.all_numbers == ("11", "12")
    assert parsed.raw["extra_gate_field"] == {"kept": True}
    lifecycle = parsed.to_merge_lifecycle_item()
    assert lifecycle.repo == "gptme/gptme"


def test_plan_default_timeout_and_deterministic_session_id(tmp_path: Path) -> None:
    first = plan_run_item(item(), config(tmp_path), monitoring_rules="rules")
    second = plan_run_item(item(), config(tmp_path), monitoring_rules="rules")
    assert first.timeout_seconds == DEFAULT_TIMEOUT
    assert first.timeout_reason == "default"
    assert first.session_id == second.session_id
    assert first.claim_key == "github:gptme/gptme#42"
    assert "gh pr checks 42 --repo gptme/gptme" in first.prompt
    assert "rules" in first.prompt


def test_plan_applies_timeout_tiers_and_direct_mention_constraint(
    tmp_path: Path,
) -> None:
    assigned = plan_run_item(item(types=["assigned_issue"]), config(tmp_path))
    greptile = plan_run_item(
        item(types=["pr_update", "greptile_needs_fix"], detail="mention"),
        config(tmp_path),
    )
    assert assigned.timeout_seconds == ASSIGNED_ISSUE_TIMEOUT
    assert greptile.timeout_seconds == GREPTILE_TIMEOUT
    assert greptile.direct_mention is True
    assert "silent NOOP is not acceptable" in greptile.prompt
    assert "Greptile Score Fix Needed" in greptile.prompt


def test_execute_does_not_run_when_claim_is_denied(tmp_path: Path) -> None:
    plan = plan_run_item(item(), config(tmp_path))
    calls: list[str] = []
    hooks = RunItemHooks(
        runner=("false",),
        claim=lambda key: calls.append(key) and False,
    )
    outcome = execute_plan(plan, item(), hooks)
    assert outcome.skipped_claimed is True
    assert outcome.exit_code == 0
    assert calls == ["github:gptme/gptme#42"]


def test_execute_abandons_acquired_claim_after_runner_returns(tmp_path: Path) -> None:
    plan = plan_run_item(item(), config(tmp_path))
    abandoned: list[str] = []
    outcome = execute_plan(
        plan,
        item(),
        RunItemHooks(
            runner=("true",),
            claim=lambda _key: True,
            abandon=abandoned.append,
        ),
    )
    assert outcome.exit_code == 0
    assert abandoned == [plan.claim_key]


def test_load_items_skips_malformed_lines(tmp_path: Path) -> None:
    work_file = tmp_path / "work.jsonl"
    work_file.write_text("not json\n" + json.dumps(item().raw) + "\n", encoding="utf-8")
    assert load_items(work_file) == [item()]


def test_cli_dry_run_emits_canonical_plan(tmp_path: Path) -> None:
    work_file = tmp_path / "work.jsonl"
    work_file.write_text(json.dumps(item().raw) + "\n", encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            "run-item",
            "--workspace",
            str(tmp_path),
            "--work-file",
            str(work_file),
            "--backend",
            "codex",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["claim_key"] == "github:gptme/gptme#42"
    assert payload[0]["backend"] == "codex"
