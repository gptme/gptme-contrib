"""Tests for AutonomousRun and autonomous dispatch helpers."""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_runloops.autonomous import (
    AutonomousRun,
    is_capable_backend,
    parse_cascade_intent,
    parse_cascade_selector_output,
    self_review_cooldown_active,
    self_review_hours_since_last,
)
from gptme_runloops.utils.execution import ExecutionResult

# --- is_capable_backend ---


def test_capable_backend_claude_code():
    assert is_capable_backend("claude-code") is True
    assert is_capable_backend("claude-code", "claude-sonnet-4-6") is True


def test_capable_backend_glm5():
    assert is_capable_backend("gptme", "glm-5.2") is True
    assert is_capable_backend("gptme", "glm-5-pro") is True


def test_incapable_backend_gptme_non_glm():
    assert is_capable_backend("gptme", "deepseek-v4-pro") is False
    assert is_capable_backend("gptme") is False


def test_incapable_backend_unknown():
    assert is_capable_backend("codex") is False
    assert is_capable_backend("") is False


# --- self_review_hours_since_last ---


def _write_self_review_state(path: Path, age_hours: float) -> None:
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    path.write_text(json.dumps({"timestamp": ts.isoformat()}))


def test_hours_since_last_recent(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=2.0)
    hours = self_review_hours_since_last(state)
    assert 1.9 < hours < 2.1


def test_hours_since_last_missing_file(tmp_path):
    assert self_review_hours_since_last(tmp_path / "no-such.json") == 999.0


def test_hours_since_last_corrupt_file(tmp_path):
    state = tmp_path / "bad.json"
    state.write_text("not json at all")
    assert self_review_hours_since_last(state) == 999.0


def test_hours_since_last_missing_timestamp(tmp_path):
    state = tmp_path / "empty.json"
    state.write_text(json.dumps({}))
    assert self_review_hours_since_last(state) == 999.0


# --- self_review_cooldown_active ---


def test_cooldown_active_when_recent(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=3.0)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is True


def test_cooldown_inactive_when_old(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=8.0)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is False


def test_cooldown_inactive_when_missing(tmp_path):
    assert self_review_cooldown_active(tmp_path / "no-such.json") is False


def test_cooldown_boundary(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=6.0)
    # Exactly at boundary → NOT active (>= 6h means cooldown cleared)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is False


# --- CLI exit codes ---


def test_cli_is_capable_backend_exit0():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "is-capable-backend",
            "claude-code",
        ],
        capture_output=True,
    )
    assert result.returncode == 0


def test_cli_is_capable_backend_exit1():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "is-capable-backend",
            "codex",
        ],
        capture_output=True,
    )
    assert result.returncode == 1


def test_cli_self_review_cooldown_active(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=2.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-cooldown",
            str(state),
        ],
        capture_output=True,
    )
    assert result.returncode == 0  # exit 0 = on cooldown


def test_cli_self_review_cooldown_inactive(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=10.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-cooldown",
            str(state),
        ],
        capture_output=True,
    )
    assert result.returncode == 1  # exit 1 = not on cooldown


def test_cli_self_review_hours(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=5.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-hours",
            str(state),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert 4.9 < float(result.stdout.strip()) < 5.1


def test_autonomous_generate_prompt():
    """Test prompt generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = AutonomousRun(workspace)
        prompt = run.generate_prompt()

        # Should contain key sections
        assert "autonomous" in prompt.lower()
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt


def test_autonomous_run_cycle():
    """Test full autonomous run cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = AutonomousRun(workspace)

        # Mock external calls (including _record_session to avoid live store writes)
        with (
            patch("gptme_runloops.base.git_pull_with_retry") as mock_pull,
            patch("gptme_runloops.utils.executor.execute_gptme") as mock_execute,
            patch.object(run, "_record_session"),
        ):
            mock_pull.return_value = True
            mock_execute.return_value = ExecutionResult(exit_code=0)

            exit_code = run.run()

            assert exit_code == 0
            mock_execute.assert_called_once()


def test_autonomous_timeout():
    """Test timeout configuration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        run = AutonomousRun(workspace)

        # Should have 50-minute timeout
        assert run.timeout == 3000


# --- parse_cascade_selector_output ---


def _parse(data: dict) -> list:  # type: ignore[type-arg]
    """Parse output string back into a list of 7 tokens."""
    raw = parse_cascade_selector_output(data)
    # Split on first 6 spaces (intent_json is the 7th token and may have no spaces
    # since it uses compact separators, but guard by splitting on max 6)
    parts = raw.split(" ", 6)
    assert len(parts) == 7, f"Expected 7 tokens, got {len(parts)}: {raw!r}"
    return parts


def test_parse_cascade_standard_task():
    data = {
        "tier": 1,
        "recommended_scope": "standard",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {
            "id": "my-task",
            "category": "code",
            "state": "backlog",
            "label": "My Task",
            "reason": "highest priority",
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    assert scope == "standard"
    assert sel_id == "my-task"
    assert cat == "code"
    assert exec_cat == "code"
    assert all_blocked == "false"
    assert sel_mode == "-"  # empty sentinel
    intent = json.loads(intent_json)
    assert intent["task_id"] == "my-task"
    assert intent["task_state"] == "backlog"
    assert "highest priority" in intent["reasons"]


def test_parse_cascade_tier3_all_blocked():
    data = {
        "tier": 3,
        "recommended_scope": "quick",
        "selector_mode": "synthetic_calibration",
        "blocked_tasks": ["task-a", "task-b"],
        "selected": {
            "id": "",
            "category": "cleanup",
            "selection_mode": "synthetic_calibration",
            "execution_surface": {
                "category": "cleanup",
                "label": "cleanup surface",
            },
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    assert scope == "quick"
    assert all_blocked == "true"
    assert exec_cat == "cleanup"
    intent = json.loads(intent_json)
    assert intent["selection_mode"] == "synthetic_calibration"
    assert intent["execution_category"] == "cleanup"
    assert intent["execution_label"] == "cleanup surface"


def test_parse_cascade_tier0_assigned_issue():
    # Tier 0 = assigned GitHub issue: has id but no task 'state'.
    # task_id should appear in intent; task_state must NOT.
    data = {
        "tier": 0,
        "recommended_scope": "standard",
        "selector_mode": "task_backed",
        "blocked_tasks": [],
        "selected": {
            "id": "owner/repo#42",
            "category": "code",
            "label": "Fix something",
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    assert sel_id == "owner/repo#42"
    assert all_blocked == "false"
    intent = json.loads(intent_json)
    assert intent["task_id"] == "owner/repo#42"
    assert "task_state" not in intent


def test_parse_cascade_optional_task_fields():
    data = {
        "tier": 1,
        "recommended_scope": "extended",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {
            "id": "task-x",
            "category": "infrastructure",
            "state": "todo",
            "state_flow": "activate_before_execution",
            "next_action": "Do the thing",
            "entry_actions": ["Open task file", "Claim it"],
            "upstream_coordination_id": "github:org/repo#5",
            "suggested_claim": "cascade:task:task-x",
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    assert scope == "extended"
    intent = json.loads(intent_json)
    assert intent["task_state_flow"] == "activate_before_execution"
    assert intent["task_next_action"] == "Do the thing"
    assert intent["task_entry_actions"] == ["Open task file", "Claim it"]
    assert intent["upstream_coordination_id"] == "github:org/repo#5"
    assert intent["suggested_claim"] == "cascade:task:task-x"


def test_parse_cascade_routing_hint_propagates():
    """routing_hint=haiku (from complexity_tier=low) must flow through to intent."""
    data = {
        "tier": 1,
        "recommended_scope": "standard",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {
            "id": "simple-task",
            "category": "cleanup",
            "state": "backlog",
            "complexity_tier": "low",
            "routing_hint": "haiku",
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    intent = json.loads(intent_json)
    assert intent.get("routing_hint") == "haiku"


def test_parse_cascade_routing_hint_absent_when_not_set():
    """routing_hint must NOT appear in intent when the selector omits it.

    Note: the tier→hint decision belongs to the upstream cascade-selector, not
    here. ``parse_cascade_selector_output`` never reads ``complexity_tier``; it
    only propagates ``routing_hint`` when the selector sets it. The
    ``complexity_tier`` below is realistic fixture context, not a gate.
    """
    data = {
        "tier": 1,
        "recommended_scope": "standard",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {
            "id": "complex-task",
            "category": "code",
            "state": "backlog",
            "complexity_tier": "high",
        },
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    intent = json.loads(intent_json)
    assert "routing_hint" not in intent


def test_parse_cascade_minimal_empty_selected():
    # Degenerate case: empty selector output
    data: dict = {  # type: ignore[type-arg]
        "tier": 3,
        "recommended_scope": "standard",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {},
    }
    scope, sel_id, cat, exec_cat, all_blocked, sel_mode, intent_json = _parse(data)
    assert scope == "standard"
    assert sel_id == ""
    assert cat == ""
    assert exec_cat == ""
    assert all_blocked == "false"  # tier 3 but blocked_tasks is empty
    assert sel_mode == "-"
    intent = json.loads(intent_json)
    assert intent["reasons"] == []
    assert intent["tier"] == 3


def test_parse_cascade_cli_round_trip():
    """The parse-cascade-json CLI subcommand should produce identical output."""
    data = {
        "tier": 1,
        "recommended_scope": "standard",
        "selector_mode": "",
        "blocked_tasks": [],
        "selected": {
            "id": "round-trip-task",
            "category": "code",
            "state": "backlog",
            "reasons": ["testing"],
        },
    }
    expected = parse_cascade_selector_output(data)
    result = subprocess.run(
        [sys.executable, "-m", "gptme_runloops.autonomous", "parse-cascade-json"],
        input=json.dumps(data).encode(),
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout.decode().strip() == expected


def test_bob_runtime_invokes_parse_cascade_cli():
    """The production caller must use this package instead of inline parsing."""
    runtime = (
        Path(__file__).parents[4]
        / "scripts"
        / "runs"
        / "autonomous"
        / "autonomous-run.sh"
    )
    if not runtime.exists():
        pytest.skip(
            "autonomous-run.sh not present; adoption verified in agent-workspace CI"
        )

    source = runtime.read_text()
    integration = source[source.index("_CASCADE_PRESELECT_AGENT=") :]
    integration = integration[: integration.index("_apply_cascade_routing")]
    assert "-m gptme_runloops.autonomous parse-cascade-json" in integration
    assert "json.load(sys.stdin)" not in integration


# --- parse_cascade_intent ---


def test_parse_cascade_intent_full():
    data = {
        "task_id": "my-task",
        "execution_category": "code",
        "upstream_coordination_id": "github:owner/repo#42",
        "task_state": "active",
    }
    result = parse_cascade_intent(data)
    assert result["task_id"] == "my-task"
    assert result["execution_category"] == "code"
    assert result["upstream_coordination_id"] == "github:owner/repo#42"
    assert result["task_state"] == "active"


def test_parse_cascade_intent_empty_dict():
    result = parse_cascade_intent({})
    assert result == {
        "task_id": "",
        "execution_category": "",
        "upstream_coordination_id": "",
        "task_state": "",
    }


def test_parse_cascade_intent_partial():
    result = parse_cascade_intent({"task_id": "foo", "task_state": "todo"})
    assert result["task_id"] == "foo"
    assert result["task_state"] == "todo"
    assert result["execution_category"] == ""
    assert result["upstream_coordination_id"] == ""


def test_parse_cascade_intent_none_values():
    # Explicit None → empty string (mirrors the `or ''` in the bash inline blocks)
    result = parse_cascade_intent(
        {
            "task_id": None,
            "execution_category": None,
            "upstream_coordination_id": None,
            "task_state": None,
        }
    )
    assert all(v == "" for v in result.values())


def test_parse_cascade_intent_returns_strings():
    # All values must be plain strings regardless of input type
    result = parse_cascade_intent({"task_id": 42, "task_state": True})
    assert isinstance(result["task_id"], str)
    assert isinstance(result["task_state"], str)


# --- parse-cascade-intent CLI ---


def test_cli_parse_cascade_intent_full():
    data = {
        "task_id": "test-task",
        "execution_category": "infrastructure",
        "upstream_coordination_id": "github:gptme/gptme#123",
        "task_state": "todo",
    }
    result = subprocess.run(
        [sys.executable, "-m", "gptme_runloops.autonomous", "parse-cascade-intent"],
        input=json.dumps(data),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    parts = result.stdout.strip().split(" ", 3)
    assert len(parts) == 4
    assert parts[0] == "test-task"
    assert parts[1] == "infrastructure"
    assert parts[2] == "github:gptme/gptme#123"
    assert parts[3] == "todo"


def test_cli_parse_cascade_intent_empty_json():
    result = subprocess.run(
        [sys.executable, "-m", "gptme_runloops.autonomous", "parse-cascade-intent"],
        input="{}",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # 4 empty tokens → 3 spaces; bash `read -r` will assign all as empty strings
    assert result.stdout.rstrip("\n") == "   "


def test_cli_parse_cascade_intent_invalid_json():
    result = subprocess.run(
        [sys.executable, "-m", "gptme_runloops.autonomous", "parse-cascade-intent"],
        input="not json",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0  # graceful: no crash on bad input
    assert result.stdout.rstrip("\n") == "   "  # empty fields
