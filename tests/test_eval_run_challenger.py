from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "eval-run-challenger.py"
)
spec = importlib.util.spec_from_file_location("eval_run_challenger", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
eval_run_challenger = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = eval_run_challenger
spec.loader.exec_module(eval_run_challenger)

# Loaded dynamically above, so mypy sees this as Any rather than a real type —
# used only to construct fixtures below, never as a static type annotation.
ChallengerResult = eval_run_challenger.ChallengerResult


def _write_trace_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_load_traces_skips_missing_trajectory(tmp_path: Path) -> None:
    existing = tmp_path / "trajectory.jsonl"
    existing.write_text("{}\n")
    traces_path = tmp_path / "traces.jsonl"
    _write_trace_jsonl(
        traces_path,
        [
            {"trace_id": "aaaa", "category": "code", "trajectory_path": str(existing)},
            {"trace_id": "bbbb", "category": "code", "trajectory_path": ""},
            {
                "trace_id": "cccc",
                "category": "code",
                "trajectory_path": "/nonexistent/path.jsonl",
            },
        ],
    )

    records = eval_run_challenger.load_traces(traces_path)

    assert [r["trace_id"] for r in records] == ["aaaa"]


def test_load_traces_filters_by_category_and_limit(tmp_path: Path) -> None:
    existing = tmp_path / "trajectory.jsonl"
    existing.write_text("{}\n")
    traces_path = tmp_path / "traces.jsonl"
    _write_trace_jsonl(
        traces_path,
        [
            {"trace_id": "aaaa", "category": "code", "trajectory_path": str(existing)},
            {
                "trace_id": "bbbb",
                "category": "research",
                "trajectory_path": str(existing),
            },
            {"trace_id": "cccc", "category": "code", "trajectory_path": str(existing)},
        ],
    )

    records = eval_run_challenger.load_traces(traces_path, category="code", limit=1)

    assert [r["trace_id"] for r in records] == ["aaaa"]


def test_extract_oracle_input_returns_first_user_message(tmp_path: Path) -> None:
    # gptme conversation.jsonl format: top-level role/content per record.
    trajectory = tmp_path / "conversation.jsonl"
    trajectory.write_text(
        "\n".join(
            [
                json.dumps({"role": "system", "content": "sys prelude"}),
                json.dumps({"role": "user", "content": "do the task"}),
                json.dumps({"role": "assistant", "content": "ok"}),
            ]
        )
        + "\n"
    )

    result = eval_run_challenger.extract_oracle_input(str(trajectory))

    assert result == "do the task"


def test_extract_oracle_input_returns_none_without_user_message(tmp_path: Path) -> None:
    trajectory = tmp_path / "conversation.jsonl"
    trajectory.write_text(json.dumps({"role": "assistant", "content": "ok"}) + "\n")

    result = eval_run_challenger.extract_oracle_input(str(trajectory))

    assert result is None


@pytest.mark.parametrize(
    "oracle,challenger,expected_commits_match,expected_outcome_match",
    [
        (
            {"commits_made": 3, "duration_seconds": 100, "outcome": "productive"},
            ChallengerResult(exit_code=0, duration_seconds=50, commits_made=2),
            True,
            True,
        ),
        (
            {"commits_made": 0, "duration_seconds": 100, "outcome": "noop"},
            ChallengerResult(exit_code=0, duration_seconds=50, commits_made=1),
            False,
            False,
        ),
        (
            {"commits_made": 2, "duration_seconds": 200, "outcome": "productive"},
            ChallengerResult(exit_code=0, duration_seconds=50, commits_made=0),
            False,
            False,
        ),
    ],
)
def test_compute_metrics(
    oracle: dict,
    challenger: Any,
    expected_commits_match: bool,
    expected_outcome_match: bool,
) -> None:
    metrics = eval_run_challenger.compute_metrics(oracle, challenger)

    assert metrics["commits_made_match"] is expected_commits_match
    assert metrics["outcome_match"] is expected_outcome_match
    assert metrics["duration_ratio"] == pytest.approx(
        challenger.duration_seconds / oracle["duration_seconds"]
    )


def test_compute_metrics_handles_zero_oracle_duration() -> None:
    metrics = eval_run_challenger.compute_metrics(
        {"commits_made": 1, "duration_seconds": 0, "outcome": "productive"},
        ChallengerResult(exit_code=0, duration_seconds=10, commits_made=1),
    )

    assert metrics["duration_ratio"] is None


def test_build_report_groups_by_category() -> None:
    results = [
        {
            "category": "code",
            "metrics": {
                "commits_made_match": True,
                "outcome_match": True,
                "composite_score": 1.0,
            },
        },
        {
            "category": "code",
            "metrics": {
                "commits_made_match": False,
                "outcome_match": False,
                "composite_score": 0.0,
            },
        },
        {
            "category": "research",
            "metrics": {
                "commits_made_match": True,
                "outcome_match": True,
                "composite_score": 1.0,
            },
        },
    ]

    report = eval_run_challenger.build_report(results)

    assert "| code | 2 | 1/2 | 1/2 | 0.50 |" in report
    assert "| research | 1 | 1/1 | 1/1 | 1.00 |" in report
