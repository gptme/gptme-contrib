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
