"""Tests for SessionRecord dataclass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.record import SessionRecord, normalize_model


def test_context_tier_default():
    """context_tier defaults to None."""
    r = SessionRecord()
    assert r.context_tier is None
    assert r.grades == {}
    assert r.grade_reasons == {}


def test_context_tier_roundtrip():
    """context_tier survives to_dict/from_dict round-trip."""
    r = SessionRecord(
        harness="claude-code",
        model="opus",
        context_tier="massive",
        outcome="productive",
    )
    d = r.to_dict()
    assert d["context_tier"] == "massive"

    r2 = SessionRecord.from_dict(d)
    assert r2.context_tier == "massive"


def test_context_tier_none_roundtrip():
    """context_tier=None round-trips correctly."""
    r = SessionRecord(model="sonnet")
    d = r.to_dict()
    assert d["context_tier"] is None

    r2 = SessionRecord.from_dict(d)
    assert r2.context_tier is None


def test_context_tier_in_json():
    """context_tier appears in JSON output."""
    import json

    r = SessionRecord(context_tier="extended")
    parsed = json.loads(r.to_json())
    assert parsed["context_tier"] == "extended"


def test_ab_group_default():
    """ab_group defaults to None."""
    r = SessionRecord()
    assert r.ab_group is None


def test_tier_version_default():
    """tier_version defaults to None."""
    r = SessionRecord()
    assert r.tier_version is None


def test_ab_group_tier_version_roundtrip():
    """ab_group and tier_version survive to_dict/from_dict/to_json round-trip."""
    import json

    r = SessionRecord(
        harness="claude-code",
        model="opus",
        context_tier="massive",
        ab_group="treatment",
        tier_version="v2",
        outcome="productive",
    )
    # to_dict
    d = r.to_dict()
    assert d["ab_group"] == "treatment"
    assert d["tier_version"] == "v2"

    # from_dict
    r2 = SessionRecord.from_dict(d)
    assert r2.ab_group == "treatment"
    assert r2.tier_version == "v2"

    # to_json / from_dict
    parsed = json.loads(r.to_json())
    assert parsed["ab_group"] == "treatment"
    assert parsed["tier_version"] == "v2"
    r3 = SessionRecord.from_dict(parsed)
    assert r3.ab_group == "treatment"
    assert r3.tier_version == "v2"


def test_ab_group_tier_version_none_roundtrip():
    """ab_group=None and tier_version=None round-trip correctly."""
    r = SessionRecord(model="sonnet")
    d = r.to_dict()
    assert d["ab_group"] is None
    assert d["tier_version"] is None

    r2 = SessionRecord.from_dict(d)
    assert r2.ab_group is None
    assert r2.tier_version is None


# -- project and session_name fields -----------------------------------------


def test_project_default():
    """project defaults to None."""
    r = SessionRecord()
    assert r.project is None


def test_session_name_default():
    """session_name defaults to None."""
    r = SessionRecord()
    assert r.session_name is None


def test_project_session_name_roundtrip():
    """project and session_name survive to_dict/from_dict round-trip."""
    import json

    r = SessionRecord(
        harness="claude-code",
        model="opus",
        project="/Users/erb/myproject",
        session_name="dancing-blue-fish",
        outcome="productive",
    )
    d = r.to_dict()
    assert d["project"] == "/Users/erb/myproject"
    assert d["session_name"] == "dancing-blue-fish"

    r2 = SessionRecord.from_dict(d)
    assert r2.project == "/Users/erb/myproject"
    assert r2.session_name == "dancing-blue-fish"

    # JSON round-trip
    parsed = json.loads(r.to_json())
    assert parsed["project"] == "/Users/erb/myproject"
    assert parsed["session_name"] == "dancing-blue-fish"
    r3 = SessionRecord.from_dict(parsed)
    assert r3.project == "/Users/erb/myproject"
    assert r3.session_name == "dancing-blue-fish"


def test_project_session_name_none_roundtrip():
    """project=None and session_name=None round-trip correctly."""
    r = SessionRecord(model="sonnet")
    d = r.to_dict()
    assert d["project"] is None
    assert d["session_name"] is None

    r2 = SessionRecord.from_dict(d)
    assert r2.project is None
    assert r2.session_name is None


def test_grade_helpers_sync_multivariate_and_legacy_fields():
    """Helper methods keep new grade dicts aligned with legacy scalar fields."""
    r = SessionRecord(session_id="grades1")

    r.set_productivity_grade(0.72)
    r.set_alignment_grade(
        0.84,
        reason="Strong work on the active priority.",
        model="claude-haiku-4-5",
    )

    assert r.trajectory_grade == 0.72
    assert r.llm_judge_score == 0.84
    assert r.llm_judge_reason == "Strong work on the active priority."
    assert r.llm_judge_model == "claude-haiku-4-5"
    assert r.grades == {"productivity": 0.72, "alignment": 0.84}
    assert r.grade_reasons == {"alignment": "Strong work on the active priority."}


def test_sync_grade_fields_backfills_missing_multivariate_fields():
    """Legacy scalar fields can backfill missing multivariate grade entries."""
    r = SessionRecord(
        session_id="legacy-grade-sync",
        trajectory_grade=0.66,
        llm_judge_score=0.81,
        llm_judge_reason="Useful progress on the active task.",
        llm_judge_model="claude-haiku-4-5",
    )

    changed = r.sync_grade_fields()

    assert changed is True
    assert r.grades == {"productivity": 0.66, "alignment": 0.81}
    assert r.grade_reasons == {"alignment": "Useful progress on the active task."}
    assert r.sync_grade_fields() is False


# -- compute_trajectory_grade and apply_weighted_grade -----------------------


def test_compute_trajectory_grade_basic():
    """Weighted average uses only dims present in both grades and weights."""
    from gptme_sessions.record import compute_trajectory_grade

    grades = {"productivity": 0.8, "alignment": 0.6}
    weights = {"productivity": 0.4, "alignment": 0.35, "harm": 0.25}
    # harm missing from grades — only productivity + alignment contribute
    expected = (0.8 * 0.4 + 0.6 * 0.35) / (0.4 + 0.35)
    assert abs(compute_trajectory_grade(grades, weights) - expected) < 1e-9


def test_compute_trajectory_grade_all_dims():
    """All three dims present — full weighted average."""
    from gptme_sessions.record import compute_trajectory_grade

    grades = {"productivity": 0.8, "alignment": 0.7, "harm": 1.0}
    weights = {"productivity": 0.4, "alignment": 0.35, "harm": 0.25}
    expected = (0.8 * 0.4 + 0.7 * 0.35 + 1.0 * 0.25) / 1.0
    assert abs(compute_trajectory_grade(grades, weights) - expected) < 1e-9


def test_compute_trajectory_grade_no_overlap():
    """No overlapping dims → returns None."""
    from gptme_sessions.record import compute_trajectory_grade

    grades = {"novelty": 0.9}
    weights = {"productivity": 0.5, "harm": 0.5}
    assert compute_trajectory_grade(grades, weights) is None


def test_compute_trajectory_grade_empty_grades():
    """Empty grades dict → returns None."""
    from gptme_sessions.record import compute_trajectory_grade

    assert compute_trajectory_grade({}, {"productivity": 1.0}) is None


def test_apply_weighted_grade_updates_trajectory_grade():
    """apply_weighted_grade() updates trajectory_grade and returns the value."""
    r = SessionRecord(session_id="weighted-test")
    r.set_productivity_grade(0.8)
    r.grades["alignment"] = 0.6
    weights = {"productivity": 0.4, "alignment": 0.35, "harm": 0.25}

    result = r.apply_weighted_grade(weights)

    expected = (0.8 * 0.4 + 0.6 * 0.35) / (0.4 + 0.35)
    assert result is not None
    assert abs(result - expected) < 1e-9
    assert abs(r.trajectory_grade - expected) < 1e-9


def test_apply_weighted_grade_no_grades_returns_none():
    """apply_weighted_grade() with no matching dims returns None without mutating."""
    r = SessionRecord(session_id="no-weights")
    r.grades["novelty"] = 0.9
    weights = {"productivity": 1.0}

    result = r.apply_weighted_grade(weights)

    assert result is None
    assert r.trajectory_grade is None


# -- normalize_model: dot variants and regex fallback ------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Dot variants (new aliases)
        ("claude-sonnet-4.6", "sonnet"),
        ("claude-opus-4.5", "opus"),
        ("anthropic/claude-sonnet-4.6", "sonnet"),
        ("anthropic/claude-haiku-4.5", "haiku"),
        # New providers
        ("openai-subscription/gpt-5.4", "gpt-5.4"),
        ("minimax-m2.5", "minimax-m2.5"),
        ("minimax-m2.7", "minimax-m2.7"),
        ("gemini-3.1-flash", "gemini-3.1-flash"),
        ("gemini-3.1-pro", "gemini-3.1-pro"),
        ("qwen3.5-coder", "qwen3.5-coder"),
        # Existing aliases still work
        ("claude-opus-4-6", "opus"),
        ("gpt-4o", "gpt-4o"),
        ("openrouter/anthropic/claude-opus-4-6", "opus"),
    ],
)
def test_normalize_model_aliases(raw: str, expected: str) -> None:
    assert normalize_model(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Regex fallback: strip provider prefixes
        ("openrouter/deepseek/deepseek-r2", "deepseek-r2"),
        ("anthropic/claude-future-99", "claude-future-99"),
        ("openai-subscription/gpt-99", "gpt-99"),
        ("openai/gpt-future", "gpt-future"),
        ("xai/grok-99", "grok-99"),
        # Strip @provider suffixes
        ("openrouter/some-provider/some-model@some-provider", "some-model"),
        # Already short: returned as-is
        ("local-llm", "local-llm"),
        # None stays None
        (None, None),
        # Empty string returns empty string (falsy, like None)
        ("", ""),
    ],
)
def test_normalize_model_regex_fallback(raw: str | None, expected: str | None) -> None:
    assert normalize_model(raw) == expected


# -- span_aggregates field + populate_span_aggregates helper ------------------


def _write_cc_trajectory(tmp_path: Path) -> Path:
    """Synthetic CC JSONL: one Bash dispatch + successful result."""
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tid1",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-21T10:00:01+00:00",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tid1",
                        "content": "hi",
                        "is_error": False,
                    }
                ]
            },
        },
    ]
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _write_gptme_trajectory(tmp_path: Path, session_name: str = "sess-aa") -> Path:
    """Synthetic gptme conversation.jsonl: one shell dispatch + success result."""
    sess_dir = tmp_path / session_name
    sess_dir.mkdir()
    records = [
        {
            "role": "assistant",
            "timestamp": "2026-04-21T10:00:00",
            "content": '\n@shell(call-abc-0): {"command": "echo hi"}',
        },
        {
            "role": "system",
            "timestamp": "2026-04-21T10:00:01",
            "content": "Ran allowlisted command: `echo hi`\n\n```stdout\nhi\n```",
            "pinned": False,
        },
    ]
    p = sess_dir / "conversation.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_span_aggregates_default_is_none():
    """span_aggregates defaults to None."""
    r = SessionRecord()
    assert r.span_aggregates is None


def test_span_aggregates_roundtrip():
    """span_aggregates survives to_dict/from_dict and JSON round-trip."""
    agg = {
        "total_spans": 3,
        "error_spans": 1,
        "error_rate": 1 / 3,
        "dominant_tool": "Bash",
        "avg_duration_ms": 250.0,
        "max_duration_ms": 500,
        "tool_counts": {"Bash": 2, "Read": 1},
        "retry_depth": 1,
    }
    r = SessionRecord(session_id="span-rt", span_aggregates=agg)
    d = r.to_dict()
    assert d["span_aggregates"] == agg

    r2 = SessionRecord.from_dict(d)
    assert r2.span_aggregates == agg

    parsed = json.loads(r.to_json())
    assert parsed["span_aggregates"] == agg
    r3 = SessionRecord.from_dict(parsed)
    assert r3.span_aggregates == agg


def test_span_aggregates_none_roundtrip():
    """span_aggregates=None round-trips correctly."""
    r = SessionRecord()
    d = r.to_dict()
    assert d["span_aggregates"] is None

    r2 = SessionRecord.from_dict(d)
    assert r2.span_aggregates is None


def test_populate_span_aggregates_no_trajectory_path():
    """Returns False and leaves span_aggregates unchanged when trajectory_path is None."""
    r = SessionRecord(harness="claude-code")
    assert r.populate_span_aggregates() is False
    assert r.span_aggregates is None


def test_populate_span_aggregates_missing_file(tmp_path: Path):
    """Returns False when trajectory file does not exist."""
    r = SessionRecord(
        harness="claude-code",
        trajectory_path=str(tmp_path / "does-not-exist.jsonl"),
    )
    assert r.populate_span_aggregates() is False
    assert r.span_aggregates is None


def test_populate_span_aggregates_unknown_harness(tmp_path: Path):
    """Returns False for harnesses without an extractor."""
    p = _write_cc_trajectory(tmp_path)
    r = SessionRecord(harness="copilot-cli", trajectory_path=str(p))
    assert r.populate_span_aggregates() is False
    assert r.span_aggregates is None


def test_populate_span_aggregates_cc_trajectory(tmp_path: Path):
    """Populates aggregates from a claude-code trajectory."""
    p = _write_cc_trajectory(tmp_path)
    r = SessionRecord(harness="claude-code", trajectory_path=str(p))
    assert r.populate_span_aggregates() is True
    assert r.span_aggregates is not None
    assert r.span_aggregates["total_spans"] == 1
    assert r.span_aggregates["error_spans"] == 0
    assert r.span_aggregates["error_rate"] == 0.0
    assert r.span_aggregates["dominant_tool"] == "Bash"
    assert r.span_aggregates["max_duration_ms"] == 1000
    assert r.span_aggregates["tool_counts"] == {"Bash": 1}
    assert r.span_aggregates["retry_depth"] == 0


def test_populate_span_aggregates_gptme_trajectory(tmp_path: Path):
    """Populates aggregates from a gptme conversation.jsonl."""
    p = _write_gptme_trajectory(tmp_path, session_name="sess-aa")
    r = SessionRecord(harness="gptme", trajectory_path=str(p))
    assert r.populate_span_aggregates() is True
    assert r.span_aggregates is not None
    assert r.span_aggregates["total_spans"] == 1
    assert r.span_aggregates["dominant_tool"] == "shell"
    assert r.span_aggregates["error_spans"] == 0
    assert r.span_aggregates["tool_counts"] == {"shell": 1}


def test_populate_span_aggregates_harness_hint_overrides(tmp_path: Path):
    """harness_hint overrides self.harness for extractor selection."""
    p = _write_cc_trajectory(tmp_path)
    # harness is intentionally wrong; hint should still pick CC extractor.
    r = SessionRecord(harness="gptme", trajectory_path=str(p))
    assert r.populate_span_aggregates(harness_hint="claude-code") is True
    assert r.span_aggregates is not None
    assert r.span_aggregates["total_spans"] == 1
    assert r.span_aggregates["dominant_tool"] == "Bash"


def test_populate_span_aggregates_idempotent_rerun(tmp_path: Path):
    """Re-running populate replaces previous aggregates (idempotent contract)."""
    p = _write_cc_trajectory(tmp_path)
    r = SessionRecord(harness="claude-code", trajectory_path=str(p))
    assert r.populate_span_aggregates() is True
    first = dict(r.span_aggregates or {})
    assert r.populate_span_aggregates() is True
    assert r.span_aggregates == first
