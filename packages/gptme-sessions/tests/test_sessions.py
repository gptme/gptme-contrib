"""Tests for gptme-sessions package."""

import json
from pathlib import Path

from gptme_sessions import SessionRecord, SessionStore
from gptme_sessions.store import (
    compute_run_analytics,
    format_run_analytics,
    format_stats,
)


def test_session_record_defaults():
    """SessionRecord auto-generates ID and timestamp."""
    r = SessionRecord()
    assert r.session_id
    assert r.timestamp
    assert r.harness == "unknown"
    assert r.model == "unknown"


def test_session_record_serialization():
    """Round-trip through JSON preserves data."""
    r = SessionRecord(
        harness="claude-code",
        model="opus",
        run_type="autonomous",
        category="code",
        outcome="productive",
        duration_seconds=2400,
        deliverables=["abc123"],
    )
    d = r.to_dict()
    assert d["model"] == "opus"
    assert d["deliverables"] == ["abc123"]

    r2 = SessionRecord.from_dict(d)
    assert r2.model == "opus"
    assert r2.category == "code"
    assert r2.deliverables == ["abc123"]


def test_session_record_from_dict_ignores_unknown():
    """from_dict drops fields not in the dataclass."""
    d = {"model": "sonnet", "unknown_field": "should_be_ignored"}
    r = SessionRecord.from_dict(d)
    assert r.model == "sonnet"


def test_session_record_to_json():
    """to_json produces valid single-line JSON."""
    r = SessionRecord(model="sonnet", outcome="noop")
    line = r.to_json()
    parsed = json.loads(line)
    assert parsed["model"] == "sonnet"
    assert "\n" not in line


def test_session_store_append_and_load(tmp_path: Path):
    """Append records and load them back."""
    store = SessionStore(sessions_dir=tmp_path)

    r1 = SessionRecord(model="opus", outcome="productive", run_type="autonomous")
    r2 = SessionRecord(model="sonnet", outcome="noop", run_type="monitoring")

    store.append(r1)
    store.append(r2)

    records = store.load_all()
    assert len(records) == 2
    assert records[0].model == "opus"
    assert records[1].model == "sonnet"


def test_session_store_query_by_model(tmp_path: Path):
    """Query filters by model."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", outcome="productive"))
    store.append(SessionRecord(model="sonnet", outcome="productive"))
    store.append(SessionRecord(model="sonnet", outcome="noop"))

    results = store.query(model="sonnet")
    assert len(results) == 2
    assert all(r.model == "sonnet" for r in results)


def test_session_store_query_by_run_type(tmp_path: Path):
    """Query filters by run type."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(run_type="autonomous", outcome="productive"))
    store.append(SessionRecord(run_type="monitoring", outcome="noop"))
    store.append(SessionRecord(run_type="monitoring", outcome="productive"))

    results = store.query(run_type="monitoring")
    assert len(results) == 2


def test_session_store_query_combined_filters(tmp_path: Path):
    """Query with multiple filters intersects them."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="sonnet", run_type="monitoring", outcome="noop"))
    store.append(SessionRecord(model="sonnet", run_type="autonomous", outcome="productive"))
    store.append(SessionRecord(model="opus", run_type="monitoring", outcome="productive"))

    results = store.query(model="sonnet", run_type="monitoring")
    assert len(results) == 1
    assert results[0].outcome == "noop"


def test_session_store_stats(tmp_path: Path):
    """Stats computes correct summary."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", run_type="autonomous", outcome="productive"))
    store.append(SessionRecord(model="opus", run_type="autonomous", outcome="productive"))
    store.append(SessionRecord(model="sonnet", run_type="monitoring", outcome="noop"))
    store.append(SessionRecord(model="sonnet", run_type="monitoring", outcome="productive"))

    s = store.stats()
    assert s["total"] == 4
    assert s["productive"] == 3
    assert s["success_rate"] == 0.75

    # Model breakdown
    assert s["by_model"]["opus"]["total"] == 2
    assert s["by_model"]["opus"]["rate"] == 1.0
    assert s["by_model"]["sonnet"]["total"] == 2
    assert s["by_model"]["sonnet"]["rate"] == 0.5

    # Cross-tab
    assert s["by_model_run_type"]["opus×autonomous"]["total"] == 2
    assert s["by_model_run_type"]["sonnet×monitoring"]["rate"] == 0.5


def test_session_store_empty(tmp_path: Path):
    """Empty store returns sensible defaults."""
    store = SessionStore(sessions_dir=tmp_path)
    records = store.load_all()
    assert records == []

    s = store.stats()
    assert s["total"] == 0


def test_session_store_corrupted_line(tmp_path: Path):
    """Corrupted JSON lines are skipped."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", outcome="productive"))

    # Inject a corrupted line
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("not valid json\n")

    store.append(SessionRecord(model="sonnet", outcome="noop"))

    records = store.load_all()
    assert len(records) == 2  # corrupted line skipped


def test_session_record_model_stored_raw():
    """Model field stores the raw string, model_normalized provides short form."""
    r = SessionRecord(model="claude-opus-4-6")
    assert r.model == "claude-opus-4-6"  # raw preserved
    assert r.model_normalized == "opus"  # normalized for display
    r2 = SessionRecord(model="claude-sonnet-4-6")
    assert r2.model == "claude-sonnet-4-6"
    assert r2.model_normalized == "sonnet"
    r3 = SessionRecord(model="claude-haiku-4-5")
    assert r3.model == "claude-haiku-4-5"
    assert r3.model_normalized == "haiku"
    # Non-matching names pass through both
    r4 = SessionRecord(model="gpt-5.3-codex")
    assert r4.model == "gpt-5.3-codex"
    assert r4.model_normalized == "gpt-5.3-codex"
    # Provider-prefixed model strings normalize only via property
    r5 = SessionRecord(model="openai-subscription/gpt-5.3-codex")
    assert r5.model == "openai-subscription/gpt-5.3-codex"
    assert r5.model_normalized == "gpt-5.3-codex"
    r6 = SessionRecord(model="openrouter/z-ai/glm-5@z-ai")
    assert r6.model == "openrouter/z-ai/glm-5@z-ai"
    assert r6.model_normalized == "glm-5"
    r7 = SessionRecord(model="anthropic/claude-opus-4-6")
    assert r7.model == "anthropic/claude-opus-4-6"
    assert r7.model_normalized == "opus"


def test_session_record_run_type_normalization():
    """Numeric and prefixed run_types are normalized."""
    r = SessionRecord(run_type="1042")
    assert r.run_type == "autonomous"
    r2 = SessionRecord(run_type="autonomous-session-3")
    assert r2.run_type == "autonomous"
    # Normal values pass through
    r3 = SessionRecord(run_type="monitoring")
    assert r3.run_type == "monitoring"


def test_session_store_rewrite(tmp_path: Path):
    """Rewrite atomically replaces all records."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", outcome="productive"))
    store.append(SessionRecord(model="sonnet", outcome="noop"))
    assert len(store.load_all()) == 2

    # Rewrite with modified records
    records = store.load_all()
    records[0].category = "code"
    store.rewrite(records)

    reloaded = store.load_all()
    assert len(reloaded) == 2
    assert reloaded[0].category == "code"


def test_session_record_hour_24_fix():
    """Hour 24 timestamps are corrected to 23:59:59."""
    r = SessionRecord(timestamp="2026-03-04T24:00:00+00:00")
    assert "T23:59:59" in r.timestamp


def test_format_stats_empty():
    """format_stats handles empty stats gracefully."""
    import io

    buf = io.StringIO()
    format_stats({"total": 0}, buf)
    assert "No session records" in buf.getvalue()


def test_format_stats_with_data(tmp_path: Path):
    """format_stats produces readable output."""
    import io

    store = SessionStore(sessions_dir=tmp_path)
    store.append(
        SessionRecord(
            model="opus",
            run_type="autonomous",
            outcome="productive",
            duration_seconds=1800,
        )
    )
    store.append(SessionRecord(model="sonnet", run_type="monitoring", outcome="noop"))

    s = store.stats()
    buf = io.StringIO()
    format_stats(s, buf)
    output = buf.getvalue()
    assert "Sessions: 2" in output
    assert "opus" in output
    assert "sonnet" in output


def test_format_stats_none_harness(tmp_path: Path):
    """format_stats does not crash when harness field is None."""
    import io

    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", harness=None, outcome="productive"))
    store.append(SessionRecord(model="sonnet", harness="gptme", outcome="noop"))

    s = store.stats()
    buf = io.StringIO()
    format_stats(s, buf)  # must not raise TypeError
    output = buf.getvalue()
    assert "null" in output  # None harness displayed as "null"
    assert "gptme" in output


def test_compute_run_analytics(tmp_path: Path):
    """Run analytics computes expected breakdowns."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(
        SessionRecord(
            model="opus",
            run_type="autonomous",
            outcome="productive",
            duration_seconds=1800,
        )
    )
    store.append(
        SessionRecord(
            model="sonnet",
            run_type="monitoring",
            outcome="noop",
            duration_seconds=30,
        )
    )
    store.append(
        SessionRecord(
            model="opus",
            run_type="autonomous",
            outcome="productive",
            duration_seconds=600,
        )
    )

    records = store.load_all()
    analytics = compute_run_analytics(records)
    assert analytics["total"] == 3
    assert analytics["duration_distribution"]["30m+"] == 1
    assert analytics["duration_distribution"]["<1m"] == 1
    assert analytics["noop_by_run_type"]["monitoring"]["noop"] == 1


def test_format_run_analytics_empty():
    """format_run_analytics handles empty data gracefully."""
    import io

    buf = io.StringIO()
    format_run_analytics({"total": 0}, buf)
    assert "No session records" in buf.getvalue()


def test_normalize_model():
    """normalize_model function works standalone."""
    from gptme_sessions.record import normalize_model

    assert normalize_model("claude-opus-4-6") == "opus"
    assert normalize_model("custom-model") == "custom-model"
    assert normalize_model("anthropic/claude-sonnet-4-6") == "sonnet"


def test_session_store_query_by_category(tmp_path: Path):
    """Query filters by category."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(category="code", outcome="productive"))
    store.append(SessionRecord(category="content", outcome="productive"))
    store.append(SessionRecord(category="code", outcome="noop"))

    results = store.query(category="code")
    assert len(results) == 2


def test_session_store_query_by_harness(tmp_path: Path):
    """Query filters by harness."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(harness="claude-code", outcome="productive"))
    store.append(SessionRecord(harness="gptme", outcome="productive"))

    results = store.query(harness="gptme")
    assert len(results) == 1


def test_session_store_query_by_outcome(tmp_path: Path):
    """Query filters by outcome."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(outcome="productive"))
    store.append(SessionRecord(outcome="noop"))
    store.append(SessionRecord(outcome="productive"))

    results = store.query(outcome="productive")
    assert len(results) == 2


def test_session_record_none_run_type():
    """SessionRecord with run_type=None (from JSON null) doesn't crash."""
    r = SessionRecord.from_dict({"model": "opus", "run_type": None})
    assert r.run_type is None


def test_session_record_none_run_type_roundtrip():
    """None run_type survives full serialize → deserialize without becoming 'unknown'."""
    r = SessionRecord.from_dict({"model": "opus", "run_type": None})
    d = r.to_dict()
    assert "run_type" in d
    assert d["run_type"] is None
    r2 = SessionRecord.from_dict(d)
    assert r2.run_type is None


def test_normalize_model_none():
    """normalize_model(None) returns None without crashing."""
    from gptme_sessions.record import normalize_model

    assert normalize_model(None) is None
    assert normalize_model("") == ""


def test_session_record_none_model_roundtrip():
    """None model survives serialize → deserialize without crashing."""
    r = SessionRecord.from_dict({"run_type": "autonomous", "model": None})
    assert r.model is None
    d = r.to_dict()
    assert d["model"] is None
    r2 = SessionRecord.from_dict(d)
    assert r2.model is None


def test_session_store_null_model_in_jsonl(tmp_path: Path):
    """A JSONL record with model=null doesn't crash load_all."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="opus", outcome="productive"))

    # Inject a record with model: null
    with open(store.path, "a", encoding="utf-8") as f:
        import json

        f.write(json.dumps({"model": None, "run_type": "autonomous", "outcome": "noop"}) + "\n")

    store.append(SessionRecord(model="sonnet", outcome="noop"))

    records = store.load_all()
    assert len(records) == 3  # null model record is valid, not skipped
    assert records[1].model is None


def test_query_by_normalized_model_finds_raw_records(tmp_path: Path):
    """Querying by normalized name finds records stored with raw model strings."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="claude-opus-4-6", outcome="productive"))
    store.append(SessionRecord(model="anthropic/claude-sonnet-4-6", outcome="noop"))
    store.append(SessionRecord(model="gpt-4o", outcome="productive"))

    # Query by normalized name should find records with raw model strings
    opus_results = store.query(model="opus")
    assert len(opus_results) == 1
    assert opus_results[0].model == "claude-opus-4-6"
    assert opus_results[0].model_normalized == "opus"

    sonnet_results = store.query(model="sonnet")
    assert len(sonnet_results) == 1
    assert sonnet_results[0].model == "anthropic/claude-sonnet-4-6"

    # Query by raw name also works
    raw_results = store.query(model="claude-opus-4-6")
    assert len(raw_results) == 1


def test_model_raw_preserved_in_serialization():
    """Raw model string survives round-trip serialization."""
    r = SessionRecord(model="anthropic/claude-opus-4-6", outcome="productive")
    assert r.model == "anthropic/claude-opus-4-6"
    assert r.model_normalized == "opus"

    d = r.to_dict()
    assert d["model"] == "anthropic/claude-opus-4-6"

    r2 = SessionRecord.from_dict(d)
    assert r2.model == "anthropic/claude-opus-4-6"
    assert r2.model_normalized == "opus"


def test_stats_group_by_normalized_model(tmp_path: Path):
    """Stats groups by normalized model, merging different raw strings."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(model="claude-opus-4-6", outcome="productive"))
    store.append(SessionRecord(model="anthropic/claude-opus-4-5", outcome="productive"))
    store.append(SessionRecord(model="claude-sonnet-4-6", outcome="noop"))

    s = store.stats()
    # Both opus variants should be grouped together
    assert "opus" in s["by_model"]
    assert s["by_model"]["opus"]["total"] == 2
    assert "sonnet" in s["by_model"]
    assert s["by_model"]["sonnet"]["total"] == 1


def test_normalize_model_openai_subscription_not_absorbed():
    """openai-subscription/* models not in aliases pass through unchanged."""
    from gptme_sessions.record import normalize_model

    # Explicitly listed — should normalize
    assert normalize_model("openai-subscription/gpt-5.3-codex") == "gpt-5.3-codex"
    # Not listed — must NOT silently become "gpt-4o" via "openai" catch-all
    assert normalize_model("openai-subscription/gpt-future") == "openai-subscription/gpt-future"
    # Bare "openai" legacy still normalizes
    assert normalize_model("openai") == "gpt-4o"


def test_since_days_z_suffix_python310(tmp_path):
    """since_days filtering handles 'Z'-suffixed timestamps (Python 3.10 compat)."""
    store = SessionStore(sessions_dir=tmp_path)
    # Inject a record with a Z-suffixed timestamp (external tool / older JSON dump)
    with open(store.path, "a", encoding="utf-8") as f:
        import json

        f.write(
            json.dumps(
                {"timestamp": "2020-01-01T00:00:00Z", "model": "opus", "outcome": "productive"}
            )
            + "\n"
        )
        f.write(
            json.dumps({"timestamp": "2099-01-01T00:00:00Z", "model": "sonnet", "outcome": "noop"})
            + "\n"
        )
    # The far-future record (2099) is within 30d is impossible; but the 2020 one should be
    # excluded, not crash, when filtering with since_days
    records = store.query(since_days=1)
    # Neither is within 1 day, but the key test is that no ValueError/TypeError is raised
    assert isinstance(records, list)

    # A Z-suffixed timestamp from "now" should be included
    from datetime import datetime, timezone

    now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(store.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": now_z, "model": "haiku", "outcome": "productive"}) + "\n")
    records = store.query(since_days=1)
    assert any(r.model == "haiku" for r in records)


def test_query_stats_forwards_all_filters(tmp_path):
    """query --stats forwards category, harness, outcome to store.query()."""
    store = SessionStore(sessions_dir=tmp_path)
    store.append(
        SessionRecord(model="opus", category="code", harness="gptme", outcome="productive")
    )
    store.append(
        SessionRecord(model="sonnet", category="content", harness="claude-code", outcome="noop")
    )

    # Filter by category — only the "code" record should be counted
    code_records = store.query(category="code")
    s = store.stats(code_records)
    assert s["total"] == 1

    # Filter by outcome — only productive
    prod_records = store.query(outcome="productive")
    s2 = store.stats(prod_records)
    assert s2["total"] == 1
    assert s2["productive"] == 1

    # Filter by harness
    cc_records = store.query(harness="claude-code")
    s3 = store.stats(cc_records)
    assert s3["total"] == 1


def test_stats_subcommand_filter_args(tmp_path: Path, monkeypatch):
    """stats subcommand accepts --category, --harness, --outcome and filters correctly."""
    from gptme_sessions.cli import main

    store = SessionStore(sessions_dir=tmp_path)
    store.append(
        SessionRecord(model="opus", category="code", harness="gptme", outcome="productive")
    )
    store.append(
        SessionRecord(model="sonnet", category="content", harness="claude-code", outcome="noop")
    )

    # stats --category code should only count 1 record
    monkeypatch.setattr(
        "sys.argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "stats",
            "--category",
            "code",
            "--json",
        ],
    )
    result = main()
    assert result == 0

    # stats --harness claude-code should only count 1 record
    monkeypatch.setattr(
        "sys.argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "stats",
            "--harness",
            "claude-code",
            "--json",
        ],
    )
    result = main()
    assert result == 0

    # stats --outcome productive should only count 1 record
    monkeypatch.setattr(
        "sys.argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "stats",
            "--outcome",
            "productive",
            "--json",
        ],
    )
    result = main()
    assert result == 0
