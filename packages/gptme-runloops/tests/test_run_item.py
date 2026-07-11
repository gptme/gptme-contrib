"""Tests for the PM one-item plan and subprocess boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    prepare_monitoring_trajectory_snapshot,
    resolve_monitoring_trajectory,
    write_claude_rate_limit_block,
)
from gptme_runloops.worker_records import RateLimitRejection


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


def test_execute_resolves_trajectory_and_blocks_confirmed_rate_limit(
    tmp_path: Path,
) -> None:
    plan = plan_run_item(item(), config(tmp_path, backend="claude-code"))
    trajectory = tmp_path / "cc.jsonl"
    trajectory.write_text(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "seven_day_sonnet",
                    "resetsAt": 123,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    blocked: list[RateLimitRejection] = []
    outcome = execute_plan(
        plan,
        item(),
        RunItemHooks(
            runner=("false",),
            claim=lambda _key: True,
            abandon=lambda _key: None,
            resolve_trajectory=lambda *_args: trajectory,
            trajectory_lines=lambda path: path.read_text(encoding="utf-8").splitlines(),
            rate_limit_block=blocked.append,
        ),
    )
    assert outcome.exit_code == 1
    assert outcome.trajectory_path == trajectory
    assert blocked == [RateLimitRejection("seven_day_sonnet", 123)]


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


def test_resolve_monitoring_trajectory_from_claude_session_ref(tmp_path: Path) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    trajectory = tmp_path / "stream.jsonl"
    trajectory.write_text("x" * 5001, encoding="utf-8")
    (tmp_dir / "cc-session-log-ref-session-1.txt").write_text(
        str(trajectory), encoding="utf-8"
    )
    assert (
        resolve_monitoring_trajectory(
            "claude-code", "session-1", 100, tmp_path, tmp_dir
        )
        == trajectory
    )


def test_prepare_monitoring_trajectory_snapshot_for_codex(tmp_path: Path) -> None:
    tmp_dir = tmp_path / "tmp"
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "11"
    tmp_dir.mkdir(parents=True)
    sessions.mkdir(parents=True)
    trajectory = sessions / "rollout-1.jsonl"
    trajectory.write_text("{}\n", encoding="utf-8")
    prepare_monitoring_trajectory_snapshot("codex", "session-1", home, tmp_dir)
    assert (tmp_dir / "codex-pre-snapshot-session-1.txt").read_text(
        encoding="utf-8"
    ) == f"{trajectory}\n"


def test_resolve_monitoring_trajectory_from_copilot_snapshot(tmp_path: Path) -> None:
    tmp_dir = tmp_path / "tmp"
    home = tmp_path / "home"
    state = home / ".copilot" / "session-state"
    tmp_dir.mkdir(parents=True)
    old = state / "old"
    new = state / "new"
    old.mkdir(parents=True)
    new.mkdir()
    (old / "events.jsonl").write_text("old", encoding="utf-8")
    candidate = new / "events.jsonl"
    candidate.write_text("new", encoding="utf-8")
    (tmp_dir / "copilot-pre-snapshot-session-1.txt").write_text(
        "old\n", encoding="utf-8"
    )
    assert (
        resolve_monitoring_trajectory("copilot-cli", "session-1", 0, home, tmp_dir)
        == candidate
    )
    assert not (tmp_dir / "copilot-pre-snapshot-session-1.txt").exists()


def test_write_claude_rate_limit_block_per_sub_and_sonnet(tmp_path: Path) -> None:
    path = write_claude_rate_limit_block(
        RateLimitRejection("seven_day_sonnet", 123),
        tmp_path,
        credential_target="/x/.credentials.json.alpha",
    )
    assert path.name == "claude-code-alpha-sonnet-rate-limited-until.txt"
    assert path.read_text(encoding="utf-8").startswith("1970-01-01T00:02:03")


def test_write_claude_rate_limit_block_unknown_reset_defaults_to_6h(
    tmp_path: Path,
) -> None:
    path = write_claude_rate_limit_block(
        RateLimitRejection("requests", 0),
        tmp_path,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert path.name == "claude-code-rate-limited-until.txt"
    assert path.read_text(encoding="utf-8") == "2026-01-01T06:00:00+00:00"
