"""Tests for SessionRecord dataclass."""

from gptme_sessions.record import SessionRecord


def test_context_tier_default():
    """context_tier defaults to None."""
    r = SessionRecord()
    assert r.context_tier is None


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
