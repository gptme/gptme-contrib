"""Tests for SessionRecord dataclass."""

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
