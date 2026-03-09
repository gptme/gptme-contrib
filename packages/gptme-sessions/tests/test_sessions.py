"""Tests for gptme-sessions package."""

import json
from pathlib import Path

from gptme_sessions import SessionRecord, SessionStore
import pytest

from gptme_sessions.signals import (
    _detect_format,
    detect_format,
    extract_signals,
    extract_signals_cc,
    extract_signals_codex,
    extract_signals_copilot,
    extract_usage_cc,
    extract_usage_codex,
    extract_usage_gptme,
    grade_signals,
    infer_category,
    is_productive,
)
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


def test_store_rewrite_preserves_appended_records(tmp_path: Path):
    """rewrite() keeps records appended after load_all() was called."""
    store = SessionStore(sessions_dir=tmp_path)
    rec1 = SessionRecord(model="opus", outcome="productive")
    store.append(rec1)

    records = store.load_all()  # snapshot with only rec1

    # Simulate concurrent append between load and rewrite
    rec2 = SessionRecord(model="sonnet", outcome="noop")
    store.append(rec2)

    # Rewrite with the original snapshot (should NOT drop rec2)
    store.rewrite(records)

    reloaded = store.load_all()
    ids = {r.session_id for r in reloaded}
    assert rec1.session_id in ids
    assert rec2.session_id in ids


def test_store_rewrite_preserves_malformed_lines(tmp_path: Path):
    """rewrite() keeps malformed JSONL lines rather than silently dropping them."""
    store = SessionStore(sessions_dir=tmp_path)
    rec = SessionRecord(model="opus")
    store.append(rec)

    # Inject a malformed line directly
    with open(store.path, "a") as f:
        f.write("NOT VALID JSON\n")

    records = store.load_all()  # malformed line is skipped
    store.rewrite(records)  # should preserve the malformed line

    raw_lines = [line.strip() for line in store.path.read_text().splitlines() if line.strip()]
    assert any(line == "NOT VALID JSON" for line in raw_lines)


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


def test_session_record_none_duration_seconds():
    """duration_seconds=None (from JSON null) is coerced to 0, not left as None."""
    r = SessionRecord.from_dict({"model": "opus", "duration_seconds": None})
    assert r.duration_seconds == 0
    # Verify it doesn't crash in comparison operations used by stats/analytics
    assert r.duration_seconds > -1
    assert r.duration_seconds // 60 == 0


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


# ── Signals tests ────────────────────────────────────────────────────────────


def _make_gptme_msgs(commits: int = 0, writes: int = 0, errors: int = 0) -> list[dict]:
    """Build minimal gptme-format trajectory records."""
    msgs: list[dict] = []
    for i in range(writes):
        msgs.append(
            {
                "role": "assistant",
                "content": f'@save(c{i}): {{"path": "/home/bob/file{i}.py"}}',
                "timestamp": f"2026-03-01T10:{i:02d}:00+00:00",
            }
        )
    for i in range(commits):
        msgs.append(
            {
                "role": "system",
                "content": f"[master abc{i:04d}] commit message {i}",
                "timestamp": f"2026-03-01T11:{i:02d}:00+00:00",
            }
        )
    for _ in range(errors):
        msgs.append(
            {
                "role": "system",
                "content": "Error during execution: something went wrong",
                "timestamp": "2026-03-01T10:20:00+00:00",
            }
        )
    return msgs


def _make_cc_msgs(commits: int = 0, writes: int = 0, errors: int = 0) -> list[dict]:
    """Build minimal Claude Code-format trajectory records."""
    msgs: list[dict] = []
    for i in range(writes):
        msgs.append(
            {
                "type": "assistant",
                "timestamp": f"2026-03-01T10:{i:02d}:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"edit_{i}",
                            "name": "Edit",
                            "input": {"file_path": f"/home/bob/file{i}.py"},
                        }
                    ],
                },
            }
        )
    for i in range(commits):
        # Proper linked Bash tool_use → tool_result pair for commit detection
        bash_id = f"bash_commit_{i}"
        msgs.append(
            {
                "type": "assistant",
                "timestamp": f"2026-03-01T11:{i:02d}:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": bash_id,
                            "name": "Bash",
                            "input": {"command": f"git commit -m 'commit message {i}'"},
                        }
                    ],
                },
            }
        )
        msgs.append(
            {
                "type": "user",
                "timestamp": f"2026-03-01T11:{i:02d}:30.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": bash_id,
                            "is_error": False,
                            "content": f"[master abc{i:04d}] commit message {i}\n 1 file changed",
                        }
                    ],
                },
            }
        )
    for j in range(errors):
        msgs.append(
            {
                "type": "user",
                "timestamp": "2026-03-01T10:20:00.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "Exit code 1\nSomething failed",
                        }
                    ],
                },
            }
        )
    return msgs


def test_detect_format_gptme():
    """Detects gptme format from role-keyed messages."""
    msgs = _make_gptme_msgs(commits=1)
    assert detect_format(msgs) == "gptme"


def test_detect_format_claude_code():
    """Detects claude_code format from type-keyed records."""
    msgs = _make_cc_msgs(commits=1)
    assert detect_format(msgs) == "claude_code"


def test_detect_format_empty():
    """Empty messages default to gptme."""
    assert detect_format([]) == "gptme"


def test_extract_signals_cc_commits():
    """CC trajectory: git commits extracted from Bash tool results."""
    msgs = _make_cc_msgs(commits=2)
    sigs = extract_signals_cc(msgs)
    assert len(sigs["git_commits"]) == 2
    assert sigs["git_commits"][0].startswith("commit message 0")


def test_extract_signals_cc_file_writes():
    """CC trajectory: Edit tool calls register as file writes."""
    msgs = _make_cc_msgs(writes=3)
    sigs = extract_signals_cc(msgs)
    assert len(sigs["file_writes"]) == 3


def test_extract_signals_cc_errors():
    """CC trajectory: tool_result with is_error=True increments error_count."""
    msgs = _make_cc_msgs(errors=2)
    sigs = extract_signals_cc(msgs)
    assert sigs["error_count"] == 2


def test_extract_signals_cc_journal_excluded():
    """CC trajectory: file writes to /journal/ paths are excluded."""
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/home/bob/bob/journal/2026-03-01/session.md"},
                    }
                ],
            },
        }
    ]
    sigs = extract_signals_cc(msgs)
    assert len(sigs["file_writes"]) == 0


def test_extract_signals_cc_tool_call_counts():
    """CC trajectory: tool call names are counted correctly."""
    msgs = _make_cc_msgs(writes=2)
    msgs.append(
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:30:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
            },
        }
    )
    sigs = extract_signals_cc(msgs)
    assert sigs["tool_calls"].get("Edit", 0) == 2
    assert sigs["tool_calls"].get("Bash", 0) == 1


def test_extract_signals_cc_retry_detection():
    """CC trajectory: writing the same file twice registers as a retry."""
    path = "/home/bob/bob/script.py"
    msgs = [
        {
            "type": "assistant",
            "timestamp": f"2026-03-01T10:{i:02d}:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}],
            },
        }
        for i in range(2)
    ]
    sigs = extract_signals_cc(msgs)
    assert sigs["retry_count"] >= 1


def test_grade_signals_dead_session():
    """Grade is very low (0.10) for dead sessions with zero tool calls."""
    sigs = extract_signals_cc([])
    assert grade_signals(sigs) == 0.10


def test_grade_signals_noop():
    """Grade is 0.25 when agent was active (made tool calls) but produced no deliverables."""
    sigs = {
        "git_commits": [],
        "file_writes": [],
        "error_count": 0,
        "retry_count": 0,
        "tool_calls": {"Bash": 5},  # active but unproductive
    }
    assert grade_signals(sigs) == 0.25


def test_grade_signals_productive():
    """Grade is higher when commits are present."""
    msgs = _make_cc_msgs(commits=2)
    sigs = extract_signals_cc(msgs)
    assert grade_signals(sigs) >= 0.60


def test_is_productive_cc():
    """is_productive returns True for sessions with commits."""
    msgs = _make_cc_msgs(commits=1)
    sigs = extract_signals_cc(msgs)
    assert is_productive(sigs)


def test_is_productive_cc_writes_only():
    """is_productive returns True for sessions with 2+ writes but no commits."""
    msgs = _make_cc_msgs(writes=3)
    sigs = extract_signals_cc(msgs)
    assert is_productive(sigs)


def test_extract_from_path_cc(tmp_path: Path):
    """extract_from_path auto-detects CC format from file contents."""
    from gptme_sessions.signals import extract_from_path

    trajectory_file = tmp_path / "session.jsonl"
    msgs = _make_cc_msgs(commits=1, writes=2)
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    result = extract_from_path(trajectory_file)
    assert result["format"] == "claude_code"
    assert result["productive"] is True
    assert result["grade"] >= 0.50


def test_extract_from_path_gptme(tmp_path: Path):
    """extract_from_path auto-detects gptme format from file contents."""
    from gptme_sessions.signals import extract_from_path

    trajectory_file = tmp_path / "conversation.jsonl"
    msgs = _make_gptme_msgs(commits=1)
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    result = extract_from_path(trajectory_file)
    assert result["format"] == "gptme"
    assert result["productive"] is True


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


# ---------------------------------------------------------------------------
# extract_usage_cc
# ---------------------------------------------------------------------------


def _make_cc_assistant_usage(
    input_tokens: int,
    output_tokens: int,
    cache_create: int = 0,
    cache_read: int = 0,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Minimal CC assistant record with usage info."""
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


def test_extract_usage_cc_basic():
    """Token counts are summed across all assistant turns."""
    msgs = [
        _make_cc_assistant_usage(100, 50, 200, 0),
        _make_cc_assistant_usage(10, 20, 0, 500),
    ]
    usage = extract_usage_cc(msgs)
    assert usage["input_tokens"] == 110
    assert usage["output_tokens"] == 70
    assert usage["cache_creation_tokens"] == 200
    assert usage["cache_read_tokens"] == 500
    assert usage["total_tokens"] == 880
    assert usage["model"] == "claude-sonnet-4-6"


def test_extract_usage_cc_empty():
    """Empty trajectory (no assistant turns) returns empty dict."""
    usage = extract_usage_cc([])
    assert usage == {}


def test_extract_usage_cc_ignores_non_assistant():
    """User records and other types don't contribute to token counts."""
    msgs = [
        {"type": "user", "message": {"content": [], "usage": {"input_tokens": 9999}}},
        _make_cc_assistant_usage(10, 5),
        {"type": "queue-operation", "operation": "start"},
    ]
    usage = extract_usage_cc(msgs)
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5


def test_extract_usage_cc_model_last_wins():
    """Last seen model in assistant messages is returned."""
    msgs = [
        _make_cc_assistant_usage(10, 5, model="claude-sonnet-4-6"),
        _make_cc_assistant_usage(10, 5, model="claude-opus-4-6"),
    ]
    usage = extract_usage_cc(msgs)
    assert usage["model"] == "claude-opus-4-6"


def test_extract_usage_cc_no_usage_field():
    """Assistant record without usage field doesn't crash."""
    msgs = [
        {"type": "assistant", "message": {"model": "claude-sonnet-4-6", "content": []}},
        _make_cc_assistant_usage(5, 3),
    ]
    usage = extract_usage_cc(msgs)
    assert usage["input_tokens"] == 5
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 8


def test_detect_format_cc_with_preamble():
    """CC trajectories starting with non-standard record types are still detected correctly.

    If the first 15 records are 'queue-operation', 'system_prompt', etc., the format
    detection must scan beyond them to find the real CC records.
    """
    preamble = [
        {"type": "queue-operation", "operation": "start"},
        {"type": "system_prompt", "content": "You are an assistant."},
    ] * 8  # 16 records — beyond the old first-15 window
    cc_msgs = _make_cc_msgs(commits=1)
    assert detect_format(preamble + cc_msgs) == "claude_code"


def test_extract_signals_cc_journal_no_retry_penalty():
    """Writing to a journal path multiple times does not inflate retry_count.

    Journal paths are excluded from file_writes (not deliverables), and must
    also be excluded from retry tracking so repeated journal updates don't
    penalize the session grade.
    """
    path = "/home/bob/bob/journal/2026-03-01/session.md"
    msgs = [
        {
            "type": "assistant",
            "timestamp": f"2026-03-01T10:{i:02d}:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": path}}],
            },
        }
        for i in range(3)
    ]
    sigs = extract_signals_cc(msgs)
    assert sigs["retry_count"] == 0
    assert len(sigs["file_writes"]) == 0


def test_extract_signals_cc_commit_detection_bash_only():
    """Commit patterns in non-Bash tool results (e.g. Read) are not counted.

    A Read result could contain git log output from a file, which would be a
    false positive if we applied commit detection to all tool results.
    """
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read_1",
                        "name": "Read",
                        "input": {"file_path": "/home/bob/CHANGELOG.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-03-01T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_1",
                        "is_error": False,
                        "content": "[master abc1234] some historical commit in changelog",
                    }
                ],
            },
        },
    ]
    sigs = extract_signals_cc(msgs)
    assert len(sigs["git_commits"]) == 0  # not from Bash output — must not be counted


def test_grade_signals_file_writes_deduplication():
    """Repeated edits to the same file should not inflate grade tier.

    A session editing one file 3 times (3 raw writes, 1 unique) should NOT
    score higher than a session editing a unique file once (1 raw write, 1 unique).
    Without deduplication, the repeated-writes session would hit the writes>=3
    tier (0.55) while the single-write session stays at 0.40.
    """
    sigs_repeated = {
        "git_commits": [],
        "file_writes": ["foo.py", "foo.py", "foo.py"],  # same file 3x
        "error_count": 0,
        "retry_count": 2,
        "tool_calls": {"Edit": 3},
    }
    sigs_unique = {
        "git_commits": [],
        "file_writes": ["foo.py"],  # same unique output, one write
        "error_count": 0,
        "retry_count": 0,
        "tool_calls": {"Edit": 1},
    }
    grade_repeated = grade_signals(sigs_repeated)
    grade_unique = grade_signals(sigs_unique)
    # Both have 1 unique write — repeated session should not outscore unique session
    # (repeated session gets retry penalty, unique doesn't)
    assert grade_repeated <= grade_unique


def test_extract_signals_cc_notebook_edit():
    """NotebookEdit writes are tracked via notebook_path, not file_path."""
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "nb_1",
                        "name": "NotebookEdit",
                        "input": {"notebook_path": "/home/bob/analysis.ipynb", "new_source": "x=1"},
                    }
                ],
            },
        }
    ]
    sigs = extract_signals_cc(msgs)
    assert sigs["file_writes"] == ["/home/bob/analysis.ipynb"]


def test_is_productive_deduplication():
    """is_productive uses unique file_writes count, consistent with grade_signals."""
    sigs_two_writes_same_file = {
        "git_commits": [],
        "file_writes": ["foo.py", "foo.py"],  # 2 raw writes, 1 unique
    }
    sigs_two_unique_writes = {
        "git_commits": [],
        "file_writes": ["foo.py", "bar.py"],  # 2 raw writes, 2 unique
    }
    assert not is_productive(sigs_two_writes_same_file)
    assert is_productive(sigs_two_unique_writes)


# ---------------------------------------------------------------------------
# Direct unit tests for gptme extract_signals path
# ---------------------------------------------------------------------------


def test_extract_signals_gptme_file_writes():
    """gptme trajectory: save/write/edit/patch tool calls register as file writes."""
    msgs = _make_gptme_msgs(writes=3)
    sigs = extract_signals(msgs)
    assert len(sigs["file_writes"]) == 3
    assert all(f.startswith("/home/bob/file") for f in sigs["file_writes"])


def test_extract_signals_gptme_commits():
    """gptme trajectory: git commit lines in system messages are extracted."""
    msgs = _make_gptme_msgs(commits=2)
    sigs = extract_signals(msgs)
    assert len(sigs["git_commits"]) == 2


def test_extract_signals_gptme_error_detection_guard():
    """gptme trajectory: 'Ran command:' prefix suppresses error detection.

    This tests the operator-precedence fix: the guard must apply to all three
    error-detection conditions, not just the last one.
    """
    msgs = [
        # Should NOT be counted as error — starts with "Ran command:"
        {
            "role": "system",
            "content": "Ran command: ls\nError during execution: Permission denied",
            "timestamp": "2026-03-01T10:00:00+00:00",
        },
        # Should NOT be counted as error — starts with "Ran command:"
        {
            "role": "system",
            "content": "Ran command: cat file\nError: no such file",
            "timestamp": "2026-03-01T10:01:00+00:00",
        },
        # SHOULD be counted — genuine error output (no "Ran command:" prefix)
        {
            "role": "system",
            "content": "Error during execution: command failed",
            "timestamp": "2026-03-01T10:02:00+00:00",
        },
    ]
    sigs = extract_signals(msgs)
    assert sigs["error_count"] == 1


def test_extract_signals_gptme_retry_detection():
    """gptme trajectory: writing the same file twice registers as a retry."""
    path = "/home/bob/bob/script.py"
    msgs = [
        {
            "role": "assistant",
            "content": f'@save(c{i}): {{"path": "{path}"}}',
            "timestamp": f"2026-03-01T10:{i:02d}:00+00:00",
        }
        for i in range(2)
    ]
    sigs = extract_signals(msgs)
    assert sigs["retry_count"] >= 1


def test_extract_signals_gptme_journal_excluded():
    """gptme trajectory: writes to /journal/ paths are excluded from file_writes."""
    msgs = [
        {
            "role": "assistant",
            "content": '@save(c0): {"path": "/home/bob/bob/journal/2026-03-01/session.md"}',
            "timestamp": "2026-03-01T10:00:00+00:00",
        }
    ]
    sigs = extract_signals(msgs)
    assert len(sigs["file_writes"]) == 0


def test_extract_signals_gptme_no_placeholder_inflation():
    """gptme trajectory: tool calls with unparseable paths don't inflate file_writes.

    Previously, path-extraction failures caused placeholder strings like '<save>',
    '<write>', '<patch>' to be appended to file_writes. Three different tool names
    with unparseable args would then push the session into the writes>=3 grade tier
    (0.55) even though zero real files were written.
    """
    msgs = [
        # Three different write tools, none with extractable 'path' args
        {
            "role": "assistant",
            "content": '@save(c0): {"content": "no path field here"}',
            "timestamp": "2026-03-01T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": '@write(c1): {"content": "also no path"}',
            "timestamp": "2026-03-01T10:01:00+00:00",
        },
        {
            "role": "assistant",
            "content": '@patch(c2): {"diff": "no path either"}',
            "timestamp": "2026-03-01T10:02:00+00:00",
        },
    ]
    sigs = extract_signals(msgs)
    assert len(sigs["file_writes"]) == 0
    # Grade should reflect no real output (active session, non-zero tool calls)
    assert grade_signals(sigs) == 0.25


def test_extract_signals_cc_no_placeholder_inflation():
    """CC trajectory: write tools with no extractable path don't inflate file_writes.

    Same as gptme version but for Claude Code format — a Write tool call with no
    'file_path' in its input should not append a placeholder to file_writes.
    """
    msgs = [
        {
            "type": "assistant",
            "timestamp": f"2026-03-01T10:{i:02d}:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"write_{i}",
                        "name": "Write",
                        "input": {"content": "no file_path here"},  # missing file_path
                    }
                ],
            },
        }
        for i in range(3)
    ]
    sigs = extract_signals_cc(msgs)
    assert len(sigs["file_writes"]) == 0


def test_extract_signals_gptme_steps_basic():
    """gptme trajectory: each assistant turn with tool calls counts as one step."""
    msgs = [
        # Turn 1: one tool call
        {
            "role": "assistant",
            "content": '@shell(c0): {"command": "ls"}',
            "timestamp": "2026-03-01T10:00:00+00:00",
        },
        {"role": "system", "content": "file.py", "timestamp": "2026-03-01T10:00:01+00:00"},
        # Turn 2: two tool calls (parallel) — still one step
        {
            "role": "assistant",
            "content": '@shell(c1): {"command": "git status"}\n@save(c2): {"path": "/tmp/out.py"}',
            "timestamp": "2026-03-01T10:00:02+00:00",
        },
        {"role": "system", "content": "ok", "timestamp": "2026-03-01T10:00:03+00:00"},
        # Turn 3: pure text, no tool calls — NOT a step
        {
            "role": "assistant",
            "content": "Here is my analysis.",
            "timestamp": "2026-03-01T10:00:04+00:00",
        },
    ]
    sigs = extract_signals(msgs)
    assert sigs["steps"] == 2


def test_extract_signals_gptme_steps_parallel_tools():
    """gptme trajectory: multiple parallel tool calls in one turn = one step."""
    msgs = [
        {
            "role": "assistant",
            "content": ("@shell(c0): {}\n@shell(c1): {}\n@shell(c2): {}\n"),
            "timestamp": "2026-03-01T10:00:00+00:00",
        }
    ]
    sigs = extract_signals(msgs)
    assert sigs["steps"] == 1
    assert sum(sigs["tool_calls"].values()) == 3


def test_extract_signals_gptme_steps_zero():
    """gptme trajectory: no tool calls → steps is 0."""
    msgs = [
        {"role": "assistant", "content": "Hello world", "timestamp": "2026-03-01T10:00:00+00:00"},
        {"role": "user", "content": "Thanks", "timestamp": "2026-03-01T10:00:01+00:00"},
    ]
    sigs = extract_signals(msgs)
    assert sigs["steps"] == 0


def test_extract_signals_cc_steps_basic():
    """CC trajectory: each assistant record with tool_use items counts as one step."""
    msgs = [
        # Step 1: single Bash tool call
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
                ],
            },
        },
        # Step 2: two tool calls in parallel — still one step
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:02.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pwd"}},
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    },
                ],
            },
        },
        # Pure text turn — NOT a step
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:04.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    sigs = extract_signals_cc(msgs)
    assert sigs["steps"] == 2


def test_extract_signals_cc_steps_parallel_tools():
    """CC trajectory: multiple parallel tool_use items in one record = one step."""
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "Bash",
                        "input": {"command": f"cmd{i}"},
                    }
                    for i in range(4)
                ],
            },
        }
    ]
    sigs = extract_signals_cc(msgs)
    assert sigs["steps"] == 1
    assert sigs["tool_calls"]["Bash"] == 4


def test_detect_format_public_alias():
    """detect_format() is the public alias for _detect_format()."""
    gptme_msgs = [{"role": "assistant", "content": "hi"}]
    cc_msgs = [{"type": "assistant", "message": {"role": "assistant", "content": []}}]
    assert detect_format(gptme_msgs) == "gptme"
    assert detect_format(cc_msgs) == "claude_code"
    # Both return the same result as the private version
    assert detect_format(gptme_msgs) == _detect_format(gptme_msgs)
    assert detect_format(cc_msgs) == _detect_format(cc_msgs)


def test_signals_cli_usage_cc(tmp_path):
    """signals --usage outputs token breakdown for CC trajectories."""
    import subprocess

    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [],
                "model": "claude-opus-4-5",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 300,
                },
            },
        }
    ]
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")

    result = subprocess.run(
        ["gptme-sessions", "signals", str(p), "--usage"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    out = result.stdout.strip()
    assert "input=100" in out
    assert "output=50" in out
    assert "cache_read=300" in out
    assert "cache_create=200" in out
    assert "total=650" in out


def test_signals_cli_usage_gptme(tmp_path):
    """signals --usage produces no output for gptme format (no embedded usage)."""
    import subprocess

    msgs = [{"role": "assistant", "content": "hello", "timestamp": "2026-03-01T10:00:00+00:00"}]
    p = tmp_path / "conversation.jsonl"
    p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")

    result = subprocess.run(
        ["gptme-sessions", "signals", str(p), "--usage"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""  # no usage data in gptme format


def test_signals_cli_usage_cc_zero(tmp_path):
    """signals --usage produces no output for CC with model but all-zero usage counters."""
    import subprocess

    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [],
                "model": "claude-opus-4-5",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    ]
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")

    result = subprocess.run(
        ["gptme-sessions", "signals", str(p), "--usage"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""  # zero total tokens → no output


# ---------------------------------------------------------------------------
# extract_usage_gptme
# ---------------------------------------------------------------------------


def test_extract_usage_gptme_metadata():
    """Token counts in msg.metadata are extracted correctly."""
    msgs = [
        {
            "role": "assistant",
            "content": "hello",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 50,
                "cost": 0.005,
            },
        },
        {
            "role": "assistant",
            "content": "world",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 200,
                "output_tokens": 30,
                "cost": 0.003,
            },
        },
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 80
    assert usage["total_tokens"] == 380
    assert abs(usage["cost"] - 0.008) < 1e-9
    assert usage["model"] == "anthropic/claude-sonnet-4-6"


def test_extract_usage_gptme_empty():
    """Empty trajectory returns empty dict."""
    assert extract_usage_gptme([]) == {}


def test_extract_usage_gptme_no_metadata():
    """Messages without metadata are skipped."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            # no metadata field
        }
    ]
    assert extract_usage_gptme(msgs) == {}


def test_extract_usage_gptme_non_assistant_ignored():
    """Token data on user/system messages must not be accumulated."""
    msgs = [
        {
            "role": "user",
            "content": "hello",
            "usage": {"input_tokens": 9999, "output_tokens": 9999, "cost": 99.0},
        },
        {
            "role": "system",
            "content": "You are a helpful assistant.",
            "metadata": {"input_tokens": 9999, "output_tokens": 9999},
        },
        {
            "role": "assistant",
            "content": "Hi!",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 50,
                "output_tokens": 10,
                "cost": 0.001,
            },
        },
    ]
    usage = extract_usage_gptme(msgs)
    # Only assistant turn should contribute
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 10
    assert abs(usage["cost"] - 0.001) < 1e-9


def test_extract_usage_gptme_last_model_wins():
    """Last-seen model is recorded (consistent with extract_usage_cc)."""
    msgs = [
        {
            "role": "assistant",
            "content": "first",
            "metadata": {
                "model": "anthropic/claude-haiku-4-5",
                "input_tokens": 100,
                "output_tokens": 20,
            },
        },
        {
            "role": "assistant",
            "content": "second",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 200,
                "output_tokens": 40,
            },
        },
    ]
    usage = extract_usage_gptme(msgs)
    # Last model should win, not first
    assert usage["model"] == "anthropic/claude-sonnet-4-6"
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 60


def test_extract_from_path_gptme_includes_usage(tmp_path: Path):
    """extract_from_path includes usage data for gptme format trajectories."""
    from gptme_sessions.signals import extract_from_path

    trajectory_file = tmp_path / "conversation.jsonl"
    msgs = [
        {
            "role": "user",
            "content": "write hello",
            "timestamp": "2026-03-01T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": '@save(c0): {"path": "/tmp/hello.py"}',
            "timestamp": "2026-03-01T10:01:00+00:00",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 500,
                "output_tokens": 100,
            },
        },
    ]
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    result = extract_from_path(trajectory_file)
    assert result["format"] == "gptme"
    assert "usage" in result
    assert result["usage"]["input_tokens"] == 500
    assert result["usage"]["output_tokens"] == 100
    assert result["usage"]["model"] == "anthropic/claude-sonnet-4-6"


def test_extract_usage_gptme_cache_tokens():
    """Cache tokens from msg.metadata are extracted and included in total."""
    msgs = [
        {
            "role": "assistant",
            "content": "response",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 200,
                "cost": 0.01,
            },
        },
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["cache_read_tokens"] == 800
    assert usage["cache_creation_tokens"] == 200
    # total_tokens includes cache tokens (consistent with extract_usage_cc)
    assert usage["total_tokens"] == 1150  # 100 + 50 + 800 + 200
    assert abs(usage["cost"] - 0.01) < 1e-9


# ---------------------------------------------------------------------------
# post_session() tests
# ---------------------------------------------------------------------------


from gptme_sessions import PostSessionResult, post_session  # noqa: E402


def test_post_session_basic(tmp_path: Path):
    """post_session records a session with minimal args."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        run_type="autonomous",
    )
    assert isinstance(result, PostSessionResult)
    assert result.record.harness == "claude-code"
    assert result.record.model == "opus"
    assert result.record.outcome == "productive"
    assert result.grade is None
    assert result.signals is None
    assert result.token_count is None

    records = store.load_all()
    assert len(records) == 1
    assert records[0].session_id == result.record.session_id


def test_post_session_outcome_from_exit_code(tmp_path: Path):
    """Non-zero exit code marks session as failed."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(store=store, harness="gptme", exit_code=1)
    assert result.record.outcome == "failed"


def test_post_session_timeout_is_noop(tmp_path: Path):
    """Exit code 124 (timeout) marks session as noop, not failed."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(store=store, harness="gptme", exit_code=124)
    assert result.record.outcome == "noop"


def test_post_session_timeout_with_productive_trajectory(tmp_path: Path):
    """exit_code=124 does NOT override productive trajectory outcome."""
    import json as _json

    traj = tmp_path / "session.jsonl"
    commit_output = "[master abc1234] feat: add feature"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "git commit -m 'feat: add feature'"},
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model": "claude-opus-4-6",
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T10:01:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": commit_output}],
            },
        },
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    result = post_session(
        store=store,
        harness="claude-code",
        exit_code=124,  # timeout
        trajectory_path=traj,
    )
    # Trajectory evidence of productive work takes priority over timeout → productive
    assert result.record.outcome == "productive"


def test_post_session_timeout_with_new_commits(tmp_path: Path):
    """exit_code=124 does NOT override productive git-comparison outcome."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        exit_code=124,  # timeout
        start_commit="aaa",
        end_commit="bbb",  # different → productive by git
    )
    # Git evidence of productive work takes priority over timeout → productive
    assert result.record.outcome == "productive"


def test_post_session_noop_from_commits(tmp_path: Path):
    """Same start/end commit → noop when no trajectory."""
    store = SessionStore(sessions_dir=tmp_path)
    sha = "abc1234"
    result = post_session(
        store=store,
        harness="gptme",
        start_commit=sha,
        end_commit=sha,
    )
    assert result.record.outcome == "noop"


def test_post_session_productive_from_commits(tmp_path: Path):
    """Different start/end commits → productive when no trajectory."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        start_commit="abc1234",
        end_commit="def5678",
    )
    assert result.record.outcome == "productive"


def test_post_session_exit_code_overrides_commits(tmp_path: Path):
    """Failed exit code takes priority over git comparison."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        exit_code=2,
        start_commit="aaa",
        end_commit="bbb",  # would be productive by git
    )
    assert result.record.outcome == "failed"


def test_post_session_with_trajectory_productive(tmp_path: Path):
    """Trajectory with commits → productive outcome, grade extracted."""
    # Write a minimal CC trajectory with a git commit in Bash output
    traj = tmp_path / "session.jsonl"
    import json as _json

    commit_output = "[master abc1234] feat: add feature"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "git commit -m 'feat: add feature'"},
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model": "claude-opus-4-6",
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T10:01:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": commit_output}],
            },
        },
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    result = post_session(
        store=store,
        harness="claude-code",
        model="opus",
        trajectory_path=traj,
        start_commit="abc",
        end_commit="abc",  # same → would be noop by git, but trajectory overrides
    )
    assert result.record.outcome == "productive"
    assert result.grade is not None
    assert result.grade > 0
    assert result.signals is not None
    assert len(result.signals["git_commits"]) == 1


def test_post_session_with_trajectory_noop(tmp_path: Path):
    """Trajectory with no commits/writes → noop outcome."""
    import json as _json

    traj = tmp_path / "session.jsonl"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "echo hello"},
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model": "claude-opus-4-6",
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T10:00:30Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hello"}],
            },
        },
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    result = post_session(
        store=store,
        harness="claude-code",
        trajectory_path=traj,
    )
    assert result.record.outcome == "noop"
    assert result.grade is not None
    assert result.grade < 0.5


def test_post_session_token_count_from_trajectory(tmp_path: Path):
    """Token count extracted from CC trajectory usage data."""
    import json as _json

    traj = tmp_path / "session.jsonl"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 100,
                },
                "model": "claude-opus-4-6",
            },
        }
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    result = post_session(
        store=store,
        harness="claude-code",
        trajectory_path=traj,
    )
    assert result.token_count == 1800  # 1000+500+200+100
    assert result.record.token_count == 1800


def test_post_session_missing_trajectory(tmp_path: Path):
    """Missing trajectory path is handled gracefully (non-fatal)."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        trajectory_path=Path("/nonexistent/session.jsonl"),
        start_commit="aaa",
        end_commit="bbb",
    )
    assert result.signals is None
    assert result.grade is None
    assert result.record.outcome == "productive"  # falls back to git comparison


def test_post_session_partial_commit_pair_no_crash(tmp_path: Path):
    """Only start_commit provided (no end_commit) must not raise UnboundLocalError.

    Regression test for: elif branch that logs warning but never assigns ``outcome``.
    """
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        start_commit="abc123",  # end_commit omitted — shell footgun
    )
    # No exception; falls through to default → productive
    assert result.record.outcome == "productive"


def test_post_session_partial_commit_pair_timeout_is_noop(tmp_path: Path):
    """Partial commit pair + timeout exit code → noop (not UnboundLocalError)."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        exit_code=124,
        end_commit="def456",  # start_commit omitted
    )
    assert result.record.outcome == "noop"


def test_post_session_explicit_deliverables_override(tmp_path: Path):
    """Explicit deliverables take priority over trajectory deliverables."""
    import json as _json

    traj = tmp_path / "session.jsonl"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/tmp/foo.py", "content": "x"},
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model": "claude-opus-4-6",
            },
        }
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    explicit = ["abc123def456"]
    result = post_session(
        store=store,
        harness="claude-code",
        trajectory_path=traj,
        deliverables=explicit,
    )
    assert result.record.deliverables == explicit


def test_post_session_deliverables_from_trajectory(tmp_path: Path):
    """When no explicit deliverables, trajectory file writes are used."""
    import json as _json

    traj = tmp_path / "session.jsonl"
    msgs = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/tmp/foo.py", "content": "x"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Edit",
                        "input": {"file_path": "/tmp/bar.py", "old_string": "a", "new_string": "b"},
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model": "claude-opus-4-6",
            },
        }
    ]
    traj.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

    store = SessionStore(sessions_dir=tmp_path / "store")
    result = post_session(
        store=store,
        harness="claude-code",
        trajectory_path=traj,
    )
    assert "/tmp/foo.py" in result.record.deliverables
    assert "/tmp/bar.py" in result.record.deliverables


def test_post_session_metadata_fields(tmp_path: Path):
    """Category, journal_path, session_id and duration are stored correctly."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        category="infrastructure",
        duration_seconds=2700,
        journal_path="/home/bob/bob/journal/2026-01-01/session.md",
        session_id="test1234",
    )
    r = result.record
    assert r.category == "infrastructure"
    assert r.duration_seconds == 2700
    assert r.journal_path == "/home/bob/bob/journal/2026-01-01/session.md"
    assert r.session_id == "test1234"


def test_post_session_cli_basic(tmp_path: Path, capsys, monkeypatch):
    """CLI post-session command records a session."""
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "post-session",
            "--harness",
            "claude-code",
            "--model",
            "opus",
            "--run-type",
            "autonomous",
            "--exit-code",
            "0",
            "--duration",
            "3000",
        ],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "outcome=productive" in captured.out

    # Verify record was written
    store = SessionStore(sessions_dir=tmp_path)
    records = store.load_all()
    assert len(records) == 1
    assert records[0].outcome == "productive"


def test_post_session_cli_noop_exit_code(tmp_path: Path, capsys, monkeypatch):
    """CLI post-session: exit code 124 → noop."""
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "post-session",
            "--harness",
            "gptme",
            "--exit-code",
            "124",
        ],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "outcome=noop" in captured.out


def test_post_session_cli_json_output(tmp_path: Path, capsys, monkeypatch):
    """CLI post-session --json outputs valid JSON with expected keys."""
    import json as _json
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "post-session",
            "--harness",
            "gptme",
            "--json",
        ],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    out = _json.loads(captured.out)
    assert "session_id" in out
    assert "outcome" in out
    assert "grade" in out
    assert "token_count" in out


# ============================================================
# Codex CLI format tests
# ============================================================


def test_detect_format_codex():
    """Detect Codex CLI trajectory format from session_meta entry."""
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48.495Z",
            "type": "session_meta",
            "payload": {
                "id": "test-session",
                "originator": "codex_exec",
                "cwd": "/home/bob/bob",
            },
        }
    ]
    assert detect_format(msgs) == "codex"
    assert _detect_format(msgs) == "codex"


def test_detect_format_codex_interactive():
    """Detect Codex interactive sessions too."""
    msgs = [
        {
            "type": "session_meta",
            "payload": {"originator": "codex_interactive"},
        }
    ]
    assert detect_format(msgs) == "codex"


def test_extract_signals_codex_basic():
    """Extract signals from a minimal Codex trajectory.

    Uses two turn_context records to model two distinct turns, each with one
    tool call. Steps should be 2 (one per turn), not 2 (one per function_call).
    """
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"id": "test", "originator": "codex_exec", "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-03-05T06:56:49Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": '{"cmd": "pwd", "workdir": "/tmp"}',
            },
        },
        {
            "timestamp": "2026-03-05T06:56:51Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Process exited with code 0\nOutput:\n/tmp",
            },
        },
        {
            "timestamp": "2026-03-05T06:56:55Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-03-05T06:57:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_2",
                "arguments": '{"cmd": "git commit -m \\"feat: add thing\\""}',
            },
        },
        {
            "timestamp": "2026-03-05T06:57:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": "Process exited with code 0\n[master abc1234] feat: add thing\n 1 file changed",
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    assert signals["tool_calls"] == {"exec_command": 2}
    assert signals["steps"] == 2  # one step per turn (bounded by turn_context records)
    assert signals["error_count"] == 0
    assert len(signals["git_commits"]) == 1
    assert "feat: add thing (abc1234)" in signals["git_commits"]
    assert signals["session_duration_s"] == 14  # 06:56:48 to 06:57:02


def test_extract_signals_codex_error_detection():
    """Detect errors from non-zero exit codes in Codex."""
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"originator": "codex_exec"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": '{"cmd": "false"}',
            },
        },
        {
            "timestamp": "2026-03-05T06:56:51Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Process exited with code 1\nOutput:\n",
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    assert signals["error_count"] == 1


def test_extract_signals_codex_file_writes():
    """Detect file writes from exec_command redirect patterns."""
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"originator": "codex_exec"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": json.dumps({"cmd": "cat > /tmp/test.py <<'EOF'\nprint('hi')\nEOF"}),
            },
        },
        {
            "timestamp": "2026-03-05T06:56:51Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Process exited with code 0",
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    assert "/tmp/test.py" in signals["file_writes"]


def test_extract_signals_codex_tee_flags():
    """tee with flags (-a) should not capture the flag as a file path."""
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"originator": "codex_exec"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": json.dumps({"cmd": "echo hello | tee -a /tmp/output.log"}),
            },
        },
        {
            "timestamp": "2026-03-05T06:56:51Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Process exited with code 0",
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    # Should capture the actual path, not the flag "-a"
    assert "-a" not in signals["file_writes"]
    assert "/tmp/output.log" in signals["file_writes"]


def test_extract_usage_codex():
    """Extract model and rate-limit info from Codex trajectory."""
    msgs = [
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {"used_percent": 8.0, "window_minutes": 300},
                    "secondary": {"used_percent": 2.0, "window_minutes": 10080},
                },
            },
        },
    ]
    usage = extract_usage_codex(msgs)
    assert usage["model"] == "gpt-5.3-codex"
    assert usage["rate_limit_primary_pct"] == 8.0
    assert usage["rate_limit_secondary_pct"] == 2.0


def test_extract_usage_codex_empty():
    """Empty dict when no model/usage data."""
    assert extract_usage_codex([]) == {}
    assert extract_usage_codex([{"type": "session_meta", "payload": {}}]) == {}


# ============================================================
# Copilot CLI format tests
# ============================================================


def test_detect_format_copilot():
    """Detect Copilot CLI trajectory format from session.start entry."""
    msgs = [
        {
            "type": "session.start",
            "data": {
                "sessionId": "test-session",
                "producer": "copilot-agent",
                "selectedModel": "claude-opus-4.6",
                "context": {"cwd": "/home/bob/bob"},
            },
            "timestamp": "2026-03-03T12:32:04.861Z",
        }
    ]
    assert detect_format(msgs) == "copilot"


def test_extract_signals_copilot_basic():
    """Extract signals from a minimal Copilot trajectory."""
    msgs: list[dict] = [
        {
            "type": "session.start",
            "data": {
                "sessionId": "test",
                "producer": "copilot-agent",
                "selectedModel": "claude-opus-4.6",
            },
            "timestamp": "2026-03-03T12:32:04Z",
        },
        {
            "type": "assistant.turn_start",
            "timestamp": "2026-03-03T12:32:05Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {
                        "toolCallId": "tc_1",
                        "name": "bash",
                        "arguments": {"command": "git status"},
                        "type": "function",
                    }
                ]
            },
            "timestamp": "2026-03-03T12:32:06Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_1",
                "success": True,
                "result": {"content": "[master def5678] docs: update README\n 1 file changed"},
            },
            "timestamp": "2026-03-03T12:32:10Z",
        },
        {
            "type": "assistant.turn_end",
            "timestamp": "2026-03-03T12:32:11Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["tool_calls"] == {"bash": 1}
    assert signals["steps"] == 1
    assert signals["error_count"] == 0
    assert len(signals["git_commits"]) == 1
    assert "docs: update README (def5678)" in signals["git_commits"]
    assert signals["session_duration_s"] == 7


def test_extract_signals_copilot_error_detection():
    """Detect errors from failed tools and session.error events."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "session.error",
            "data": {
                "errorType": "authentication",
                "message": "Not authorized",
            },
            "timestamp": "2026-03-03T12:00:01Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": [{"toolCallId": "tc_1", "name": "bash", "arguments": {}}]},
            "timestamp": "2026-03-03T12:00:02Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_1",
                "success": False,
                "result": {"content": "command not found"},
            },
            "timestamp": "2026-03-03T12:00:03Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["error_count"] == 2  # session.error + failed tool


def test_extract_signals_copilot_file_writes():
    """Detect file writes from Copilot edit tool."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {
                        "toolCallId": "tc_1",
                        "name": "edit",
                        "arguments": {
                            "path": "/home/bob/bob/README.md",
                            "old_string": "old",
                            "new_string": "new",
                        },
                        "type": "function",
                    }
                ]
            },
            "timestamp": "2026-03-03T12:00:01Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert "/home/bob/bob/README.md" in signals["file_writes"]
    assert signals["tool_calls"]["edit"] == 1


def test_extract_signals_copilot_null_result():
    """extract_signals_copilot doesn't crash when tool result is JSON null."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {"toolCallId": "tc_bash", "name": "bash", "arguments": {}},
                ]
            },
            "timestamp": "2026-03-03T12:00:01Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_bash",
                "success": False,
                "result": None,  # explicit JSON null — must not crash
            },
            "timestamp": "2026-03-03T12:00:02Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["error_count"] == 1
    assert signals["git_commits"] == []


def test_extract_signals_copilot_commit_only_from_bash():
    """Git commits only extracted from bash tool output, not other tools."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {"toolCallId": "tc_view", "name": "view", "arguments": {}},
                    {"toolCallId": "tc_bash", "name": "bash", "arguments": {}},
                ]
            },
            "timestamp": "2026-03-03T12:00:01Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_view",
                "success": True,
                "result": {"content": "[master aaa1111] fake commit from file content"},
            },
            "timestamp": "2026-03-03T12:00:02Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_bash",
                "success": True,
                "result": {"content": "[master bbb2222] real commit from bash"},
            },
            "timestamp": "2026-03-03T12:00:03Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    # Only the bash commit should be extracted
    assert len(signals["git_commits"]) == 1
    assert "real commit from bash (bbb2222)" in signals["git_commits"]


def test_extract_from_path_codex(tmp_path: Path):
    """extract_from_path correctly identifies and extracts Codex format."""
    trajectory_file = tmp_path / "rollout.jsonl"
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"id": "test", "originator": "codex_exec", "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex"},
        },
        {
            "timestamp": "2026-03-05T06:57:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": '{"cmd": "echo hi"}',
            },
        },
        {
            "timestamp": "2026-03-05T06:57:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Process exited with code 0\nOutput:\nhi",
            },
        },
    ]
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    from gptme_sessions.signals import extract_from_path

    result = extract_from_path(trajectory_file)
    assert result["format"] == "codex"
    assert result["tool_calls"]["exec_command"] == 1
    assert "usage" in result
    assert result["usage"]["model"] == "gpt-5.3-codex"


def test_extract_from_path_copilot(tmp_path: Path):
    """extract_from_path correctly identifies and extracts Copilot format."""
    trajectory_file = tmp_path / "events.jsonl"
    msgs = [
        {
            "type": "session.start",
            "data": {
                "sessionId": "test",
                "producer": "copilot-agent",
                "selectedModel": "claude-opus-4.6",
            },
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": [{"toolCallId": "tc_1", "name": "bash", "arguments": {}}]},
            "timestamp": "2026-03-03T12:00:05Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "tc_1", "success": True, "result": {"content": "ok"}},
            "timestamp": "2026-03-03T12:00:10Z",
        },
    ]
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    from gptme_sessions.signals import extract_from_path

    result = extract_from_path(trajectory_file)
    assert result["format"] == "copilot"
    assert result["tool_calls"]["bash"] == 1
    # Model extracted from session.start.selectedModel
    assert result["usage"] == {"model": "claude-opus-4.6"}


def test_extract_usage_copilot_model_change():
    """extract_usage_copilot extracts model from session.model_change events."""
    from gptme_sessions.signals import extract_usage_copilot

    msgs = [
        {
            "type": "session.start",
            "data": {"sessionId": "test", "producer": "copilot-agent"},
            "timestamp": "2026-03-04T10:50:00Z",
        },
        {
            "type": "session.model_change",
            "data": {"newModel": "claude-sonnet-4.6"},
            "timestamp": "2026-03-04T10:51:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": [{"toolCallId": "tc_1", "name": "bash"}]},
            "timestamp": "2026-03-04T10:51:05Z",
        },
    ]
    usage = extract_usage_copilot(msgs)
    assert usage == {"model": "claude-sonnet-4.6"}


def test_extract_usage_copilot_session_start_selected_model():
    """extract_usage_copilot falls back to selectedModel from session.start."""
    from gptme_sessions.signals import extract_usage_copilot

    msgs = [
        {
            "type": "session.start",
            "data": {
                "sessionId": "test",
                "producer": "copilot-agent",
                "selectedModel": "claude-opus-4.6",
            },
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": []},
        },
    ]
    usage = extract_usage_copilot(msgs)
    assert usage == {"model": "claude-opus-4.6"}


def test_extract_usage_copilot_no_model():
    """extract_usage_copilot returns empty dict when no model info available."""
    from gptme_sessions.signals import extract_usage_copilot

    msgs = [
        {
            "type": "session.start",
            "data": {"sessionId": "test", "producer": "copilot-agent"},
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": []},
        },
    ]
    usage = extract_usage_copilot(msgs)
    assert usage == {}


def test_extract_usage_copilot_multiple_model_changes():
    """extract_usage_copilot uses the last model change (matches CC/gptme behavior)."""
    from gptme_sessions.signals import extract_usage_copilot

    msgs = [
        {
            "type": "session.model_change",
            "data": {"newModel": "claude-sonnet-4.6"},
        },
        {
            "type": "session.model_change",
            "data": {"newModel": "gpt-5.4"},
        },
    ]
    usage = extract_usage_copilot(msgs)
    assert usage == {"model": "gpt-5.4"}


def test_extract_from_path_copilot_with_model(tmp_path: Path):
    """extract_from_path includes usage with model when model_change is present."""
    trajectory_file = tmp_path / "events.jsonl"
    msgs = [
        {
            "type": "session.start",
            "data": {"sessionId": "test", "producer": "copilot-agent"},
            "timestamp": "2026-03-04T10:50:00Z",
        },
        {
            "type": "session.model_change",
            "data": {"newModel": "claude-sonnet-4.6"},
            "timestamp": "2026-03-04T10:51:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": [{"toolCallId": "tc_1", "name": "bash"}]},
            "timestamp": "2026-03-04T10:51:05Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "tc_1", "success": True, "result": {"content": "ok"}},
            "timestamp": "2026-03-04T10:51:10Z",
        },
    ]
    with open(trajectory_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    from gptme_sessions.signals import extract_from_path

    result = extract_from_path(trajectory_file)
    assert result["format"] == "copilot"
    assert "usage" in result
    assert result["usage"]["model"] == "claude-sonnet-4.6"


# --- Null-guard regression tests ---


def test_detect_format_codex_null_payload():
    """_detect_format doesn't crash when Codex session_meta has payload=null."""
    msgs = [{"type": "session_meta", "payload": None}]
    # Should not raise; falls through to default format
    fmt = _detect_format(msgs)
    assert fmt in ("gptme", "claude_code", "codex", "copilot")


def test_detect_format_copilot_null_data():
    """_detect_format doesn't crash when Copilot session.start has data=null."""
    msgs = [{"type": "session.start", "data": None}]
    fmt = _detect_format(msgs)
    assert fmt in ("gptme", "claude_code", "codex", "copilot")


def test_extract_signals_codex_null_payload():
    """extract_signals_codex doesn't crash when response_item has payload=null."""
    msgs: list[dict] = [
        {"type": "session_meta", "payload": {"originator": "codex_exec"}},
        {"type": "response_item", "payload": None},  # explicit null
    ]
    signals = extract_signals_codex(msgs)
    assert signals["steps"] == 0


def test_extract_signals_codex_null_cmd():
    """cmd: null in exec_command arguments must not crash re.search."""
    msgs = [
        {
            "timestamp": "2026-03-05T06:56:48Z",
            "type": "session_meta",
            "payload": {"originator": "codex_exec"},
        },
        {
            "timestamp": "2026-03-05T06:56:50Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                # cmd is explicitly null (interrupted mid-call)
                "arguments": json.dumps({"cmd": None}),
            },
        },
    ]
    # Must not raise TypeError
    signals = extract_signals_codex(msgs)
    assert signals["file_writes"] == []


def test_extract_signals_codex_multi_file_writes():
    """re.finditer captures all write targets in a multi-output command."""
    msgs = [
        {"type": "session_meta", "payload": {"originator": "codex_exec"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": {"cmd": "command | tee /tmp/debug.log > /tmp/output.txt"},
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    assert "/tmp/debug.log" in signals["file_writes"]
    assert "/tmp/output.txt" in signals["file_writes"]


def test_extract_signals_codex_dev_null_excluded():
    """/dev/null and /dev/stderr are not counted as file writes."""
    msgs = [
        {"type": "session_meta", "payload": {"originator": "codex_exec"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": {"cmd": "cmd 2>/dev/null > /tmp/out.txt"},
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    assert "/dev/null" not in signals["file_writes"]
    assert "/tmp/out.txt" in signals["file_writes"]


def test_extract_signals_codex_comparison_operators_excluded():
    """Comparison operators (>=, <=) in heredoc content are not file writes."""
    msgs = [
        {"type": "session_meta", "payload": {"originator": "codex_exec"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": {"cmd": "python3 - <<'PY'\nif x >=2:\n    print('ok')\nPY"},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c2",
                "arguments": {"cmd": "echo 'usage >=60%' | tee /tmp/report.txt"},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c3",
                "arguments": {"cmd": "python3 -c 'if x >= threshold: pass'"},
            },
        },
    ]
    signals = extract_signals_codex(msgs)
    # >=2, >= threshold, and >=60% should NOT be treated as file redirects
    assert "=2" not in signals["file_writes"]
    assert "=" not in signals["file_writes"]  # standalone >= e.g. `if x >= threshold`
    assert "=60%" not in signals["file_writes"]
    # But real file writes should still work
    assert "/tmp/report.txt" in signals["file_writes"]
    assert len(signals["deliverables"]) == 1


def test_extract_signals_copilot_null_data_assistant():
    """extract_signals_copilot doesn't crash when assistant.message has data=null."""
    msgs: list[dict] = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {"type": "assistant.message", "data": None, "timestamp": "2026-03-03T12:00:01Z"},
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["steps"] == 0


def test_extract_signals_copilot_null_tool_requests():
    """extract_signals_copilot doesn't crash when toolRequests=null."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": None},
            "timestamp": "2026-03-03T12:00:01Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["steps"] == 0


def test_extract_signals_copilot_null_arguments():
    """Copilot write tool with arguments=null doesn't crash."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {"toolCallId": "tc1", "name": "write", "arguments": None},
                ]
            },
            "timestamp": "2026-03-03T12:00:01Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["file_writes"] == []


def test_extract_signals_copilot_null_data_tool_complete():
    """extract_signals_copilot doesn't crash when tool.execution_complete has data=null."""
    msgs: list[dict] = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {"type": "tool.execution_complete", "data": None, "timestamp": "2026-03-03T12:00:01Z"},
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["error_count"] == 0


def test_extract_signals_copilot_null_success_not_counted_as_error():
    """success: null in tool.execution_complete must not increment error_count."""
    msgs = [
        {
            "type": "session.start",
            "data": {"producer": "copilot-agent"},
            "timestamp": "2026-03-03T12:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"toolRequests": [{"toolCallId": "tc_1", "name": "bash", "arguments": {}}]},
            "timestamp": "2026-03-03T12:00:01Z",
        },
        {
            "type": "tool.execution_complete",
            # success is explicitly null (truncated/partial record)
            "data": {"toolCallId": "tc_1", "success": None, "result": None},
            "timestamp": "2026-03-03T12:00:02Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert signals["error_count"] == 0


def test_extract_usage_codex_null_payload():
    """extract_usage_codex doesn't crash when turn_context/event_msg has payload=null."""
    msgs = [
        {"type": "turn_context", "payload": None},
        {"type": "event_msg", "payload": None},
    ]
    usage = extract_usage_codex(msgs)
    assert usage == {}


def test_extract_usage_codex_null_rate_limits():
    """extract_usage_codex doesn't crash when rate_limits=null."""
    msgs = [
        {"type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": None},
        },
    ]
    usage = extract_usage_codex(msgs)
    assert usage["model"] == "gpt-5.3-codex"
    assert "rate_limit_primary_pct" not in usage


# --- CLI discover subcommand ---


def _make_codex_session(path: Path, model: str = "gpt-5.3-codex") -> None:
    """Write a minimal Codex session JSONL to *path*."""
    msgs = [
        {
            "type": "session_meta",
            "payload": {"originator": "codex_exec", "session_id": "abc"},
        },
        {
            "type": "turn_context",
            "payload": {"model": model, "task": "do stuff"},
        },
    ]
    with open(path, "w") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")


def test_cli_discover_no_sessions(tmp_path: Path, capsys, monkeypatch):
    """discover returns 0 and friendly message when no sessions exist."""
    import sys

    from gptme_sessions.cli import main

    # Point all harness dirs to empty tmp dirs
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "discover", "--since", "7d"])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "No sessions found" in captured.out


def test_cli_discover_lists_paths(tmp_path: Path, capsys, monkeypatch):
    """discover prints paths for each discovered session."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "discover", "--harness", "codex"])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert str(fake_file) in captured.out
    assert "1 session(s) found" in captured.out


def test_cli_discover_harness_filter(tmp_path: Path, capsys, monkeypatch):
    """discover --harness only calls the matching discover function."""
    import sys

    from gptme_sessions.cli import main

    gptme_called: list[int] = []
    cc_called: list[int] = []

    def _gptme_discover(*a, **kw) -> list:
        gptme_called.append(1)
        return []

    def _cc_discover(*a, **kw) -> list:
        cc_called.append(1)
        return []

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", _gptme_discover)
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", _cc_discover)
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "discover", "--harness", "codex", "--since", "7d"]
    )
    rc = main()
    assert rc == 0
    assert not gptme_called, "gptme discover should not be called when --harness codex"
    assert not cc_called, "cc discover should not be called when --harness codex"


def test_cli_discover_json_output(tmp_path: Path, capsys, monkeypatch):
    """discover --json outputs a valid JSON array."""
    import json as _json
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "discover", "--harness", "codex", "--json"])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    data = _json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["harness"] == "codex"
    assert data[0]["path"] == str(fake_file)


def test_cli_discover_with_signals(tmp_path: Path, capsys, monkeypatch):
    """discover --signals extracts grade and productivity for each session."""
    import sys

    from gptme_sessions.cli import main

    session_file = tmp_path / "codex_session.jsonl"
    _make_codex_session(session_file)
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [session_file]
    )
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "discover", "--harness", "codex", "--signals"],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    # Should include grade in output
    assert "grade=" in captured.out


def test_cli_discover_gptme_sessions_with_signals(tmp_path: Path, capsys, monkeypatch):
    """discover --signals resolves gptme session directories to conversation.jsonl.

    discover_gptme_sessions returns *directory* paths, not .jsonl files.
    The discover handler must resolve them before calling extract_from_path,
    otherwise --signals fails with IsADirectoryError.
    """
    import sys

    from gptme_sessions.cli import main

    # Create a gptme-style session directory with conversation.jsonl inside
    session_dir = tmp_path / "2026-03-06-test-session"
    session_dir.mkdir()
    conversation_file = session_dir / "conversation.jsonl"
    msgs = _make_gptme_msgs(commits=1)
    with open(conversation_file, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")

    # discover_gptme_sessions returns the directory, not the file
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [session_dir]
    )
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "discover", "--harness", "gptme", "--signals"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    # --signals must produce grade output, not "signals error: Is a directory"
    assert "grade=" in captured.out
    assert "Is a directory" not in captured.out


def test_cli_discover_invalid_since(tmp_path: Path, capsys, monkeypatch):
    """discover returns non-zero on invalid --since value."""
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "discover", "--since", "notadate"])
    rc = main()
    assert rc != 0


@pytest.mark.parametrize(
    "signals,expected",
    [
        # Empty signals → None
        ({}, None),
        # Single vote (below threshold) → None
        ({"git_commits": ["feat: add feature (abc1234)"]}, None),
        # Two feat: commits → code
        (
            {"git_commits": ["feat: add feature (abc1234)", "feat: more (def5678)"]},
            "code",
        ),
        # ci(ci): commits → infrastructure
        (
            {"git_commits": ["ci(ci): update workflow (abc1234)", "ci(ci): fix runner (def5678)"]},
            "infrastructure",
        ),
        # Scope with ×2 weight: lessons scope → knowledge overrides docs→content prefix
        (
            {
                "git_commits": [
                    "docs(lessons): add lesson (abc1234)",
                    "docs(lessons): add more (def5678)",
                ]
            },
            "knowledge",
        ),
        # Tie-break: equal votes, category with highest score wins (no crash)
        (
            {
                "git_commits": [
                    "feat: code work (abc1234)",
                    "docs: content work (def5678)",
                ]
            },
            None,  # each gets 1 vote, both below threshold
        ),
        # Relative path for lessons → knowledge (not missed by leading-slash check)
        (
            {
                "git_commits": [],
                "file_writes": ["lessons/workflow/my-lesson.md", "lessons/workflow/other.md"],
            },
            "knowledge",
        ),
        # Journal-only writes — not classifiable (operational chore, not a work category)
        (
            {
                "git_commits": [],
                "file_writes": ["journal/2026-03-06/session.md", "journal/2026-03-06/work.md"],
            },
            None,
        ),
    ],
)
def test_infer_category(signals, expected):
    assert infer_category(signals) == expected


# -- discovery fallback tests ------------------------------------------------


def test_stats_fallback_to_discovery_when_empty(tmp_path: Path, capsys, monkeypatch):
    """stats shows discovery fallback when the store is empty."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "stats"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "No session records found in store" in captured.out
    assert "claude-code" in captured.out
    assert "sync" in captured.out


def test_default_command_fallback_to_discovery_when_empty(tmp_path: Path, capsys, monkeypatch):
    """Default command (no subcommand) shows discovery fallback when store is empty."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir)])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "No session records found in store" in captured.out
    assert "gptme" in captured.out


def test_runs_fallback_to_discovery_when_empty(tmp_path: Path, capsys, monkeypatch):
    """runs shows discovery fallback when the store is empty."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "runs"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "No session records found in store" in captured.out


def test_stats_no_fallback_when_records_exist(tmp_path: Path, capsys, monkeypatch):
    """stats does not show discovery fallback when records exist in the store."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionRecord, SessionStore

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(harness="gptme", model="opus", outcome="productive"))

    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "stats"]
    )
    rc = main()
    assert rc == 0
    # _show_discovery_fallback uses click.echo() which capsys captures.
    # The absence of the fallback message confirms normal stats were shown.
    captured = capsys.readouterr()
    assert "No session records found" not in captured.out


# -- sync command tests -------------------------------------------------------


def test_sync_imports_discovered_sessions(tmp_path: Path, capsys, monkeypatch):
    """sync imports discovered sessions into the store."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "Imported 1" in captured.out

    # Verify the record was written to the store
    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    assert len(records) == 1
    assert records[0].harness == "claude-code"


def test_sync_deduplicates_on_rerun(tmp_path: Path, capsys, monkeypatch):
    """sync skips sessions already in the store."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"

    # First sync
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    main()

    # Second sync — should skip the already-imported session
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "1 unchanged" in captured.out

    # Only one record should exist
    store = SessionStore(sessions_dir=sessions_dir)
    assert len(store.load_all()) == 1


def test_sync_dry_run(tmp_path: Path, capsys, monkeypatch):
    """sync --dry-run shows what would be imported without writing."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--dry-run"],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "would import" in captured.out

    # Nothing should have been written
    store = SessionStore(sessions_dir=sessions_dir)
    assert len(store.load_all()) == 0


def test_sync_no_sessions(tmp_path: Path, capsys, monkeypatch):
    """sync reports no sessions found when discovery returns nothing."""
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "No sessions found" in captured.out


# -- annotate ----------------------------------------------------------------


def test_annotate_updates_fields(tmp_path: Path):
    """annotate amends specified fields on an existing record by session ID."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord(harness="gptme", model="unknown", outcome="unknown")
    store.append(rec)
    sid = rec.session_id

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        sid,
        "--model",
        "claude-opus-4-6",
        "--outcome",
        "productive",
        "--category",
        "code",
    ]
    rc = main()
    assert rc == 0

    records = store.load_all()
    assert len(records) == 1
    r = records[0]
    assert r.model == "claude-opus-4-6"
    assert r.outcome == "productive"
    assert r.category == "code"
    assert r.harness == "gptme"  # unchanged


def test_annotate_prefix_match(tmp_path: Path):
    """annotate resolves session by ID prefix."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord(harness="gptme", model="unknown")
    store.append(rec)
    sid = rec.session_id

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        sid[:4],  # prefix
        "--outcome",
        "noop",
    ]
    rc = main()
    assert rc == 0

    records = store.load_all()
    assert records[0].outcome == "noop"


def test_annotate_unknown_id_exits_nonzero(tmp_path: Path):
    """annotate returns non-zero when session ID prefix has no match."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord())

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "zzzzzzzz",
        "--outcome",
        "productive",
    ]
    rc = main()
    assert rc != 0


def test_annotate_empty_session_id_exits_nonzero(tmp_path: Path):
    """annotate returns non-zero when session_id is an empty string."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord(harness="gptme", outcome="unknown")
    store.append(rec)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "",  # empty string matches everything via startswith
        "--outcome",
        "productive",
    ]
    rc = main()
    assert rc != 0

    # Record must be unchanged
    records = store.load_all()
    assert records[0].outcome == "unknown"


def test_annotate_ambiguous_prefix_exits_nonzero(tmp_path: Path):
    """annotate returns non-zero when prefix matches more than one record."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    # Force two records with the same prefix by controlling session_id
    r1 = SessionRecord()
    r1.session_id = "aabb1234"
    r2 = SessionRecord()
    r2.session_id = "aabb5678"
    store.append(r1)
    store.append(r2)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "aabb",
        "--outcome",
        "productive",
    ]
    rc = main()
    assert rc != 0


def test_annotate_add_deliverable(tmp_path: Path):
    """annotate --add-deliverable appends to existing deliverables list."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord(deliverables=["existing-sha"])
    store.append(rec)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        rec.session_id,
        "--add-deliverable",
        "new-sha",
    ]
    rc = main()
    assert rc == 0

    records = store.load_all()
    assert records[0].deliverables == ["existing-sha", "new-sha"]


def test_annotate_add_deliverable_deduplicates(tmp_path: Path):
    """annotate --add-deliverable does not create duplicate entries."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord(deliverables=["existing-sha"])
    store.append(rec)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        rec.session_id,
        "--add-deliverable",
        "existing-sha",  # already present
    ]
    rc = main()
    assert rc == 0

    records = store.load_all()
    assert records[0].deliverables == ["existing-sha"]  # not duplicated


def test_annotate_json_output(tmp_path: Path, capsys):
    """annotate --json outputs the updated record as JSON."""
    import sys
    import json

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord()
    store.append(rec)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        rec.session_id,
        "--model",
        "claude-opus-4-6",
        "--json",
    ]
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["model"] == "claude-opus-4-6"
    assert data["session_id"] == rec.session_id


def test_annotate_noop_exits_nonzero(tmp_path: Path):
    """annotate without any field option returns non-zero."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord()
    store.append(rec)

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        rec.session_id,
    ]
    rc = main()
    assert rc != 0


def test_annotate_selector_mode_trigger_token_count(tmp_path: Path):
    """annotate updates selector_mode, trigger, and token_count fields."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    rec = SessionRecord()
    store.append(rec)
    sid = rec.session_id

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        sid,
        "--selector-mode",
        "scored",
        "--trigger",
        "timer",
        "--token-count",
        "42000",
    ]
    rc = main()
    assert rc == 0

    records = store.load_all()
    r = records[0]
    assert r.selector_mode == "scored"
    assert r.trigger == "timer"
    assert r.token_count == 42000


def test_annotate_run_type_normalized(tmp_path: Path):
    """annotate normalizes run_type using the same rules as SessionRecord.__post_init__."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd1234", harness="gptme", run_type="unknown"))

    # Digit-only run_type should be normalized to "autonomous"
    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd1234",
        "--run-type",
        "42",
    ]
    rc = main()
    assert rc == 0
    records = store.load_all()
    assert records[0].run_type == "autonomous"

    # autonomous-session prefix should also normalize
    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd1234",
        "--run-type",
        "autonomous-session-3",
    ]
    rc = main()
    assert rc == 0
    records = store.load_all()
    assert records[0].run_type == "autonomous"


def test_annotate_lock_file_persists(tmp_path: Path):
    """annotate leaves the .lock file on disk as a permanent sentinel."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd1234", harness="gptme"))

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd1234",
        "--model",
        "claude-sonnet-4-6",
    ]
    rc = main()
    assert rc == 0

    # Lock file must remain so all callers reuse the same inode (POSIX flock
    # correctness depends on this — deleting it breaks mutual exclusion).
    lock_path = store.path.with_name(store.path.name + ".lock")
    assert lock_path.exists(), f"Lock file {lock_path} must persist as a permanent sentinel"


def test_annotate_lock_file_persists_on_error(tmp_path: Path):
    """annotate leaves the .lock file on disk even when a ClickException is raised."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd1234", harness="gptme"))

    # Use a prefix that won't match — annotate will raise ClickException mid-operation
    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "notfound",
        "--model",
        "claude-sonnet-4-6",
    ]
    rc = main()
    assert rc != 0  # ClickException exits nonzero

    lock_path = store.path.with_name(store.path.name + ".lock")
    assert lock_path.exists(), f"Lock file {lock_path} must persist as a permanent sentinel"


def test_annotate_trigger_rejects_invalid(tmp_path: Path):
    """annotate --trigger rejects values outside the allowed set."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd1234", harness="gptme"))

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd1234",
        "--trigger",
        "timeer",  # typo — not a valid choice
    ]
    rc = main()
    assert rc != 0  # click.Choice rejects invalid value

    # Record must be unchanged
    records = store.load_all()
    assert records[0].trigger is None


def test_annotate_duration_rejects_negative(tmp_path: Path):
    """annotate --duration rejects negative values."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd1234", harness="gptme", duration_seconds=300))

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd1234",
        "--duration",
        "-1",
    ]
    rc = main()
    assert rc != 0  # click.IntRange(min=0) rejects negative value

    # Record must be unchanged (still 300, not -1)
    records = store.load_all()
    assert records[0].duration_seconds == 300


def test_annotate_token_count_rejects_negative(tmp_path: Path):
    """annotate --token-count rejects negative values."""
    import sys

    from gptme_sessions import SessionRecord, SessionStore
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(session_id="abcd5678", harness="gptme", token_count=1000))

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "abcd5678",
        "--token-count",
        "-1",
    ]
    rc = main()
    assert rc != 0  # click.IntRange(min=0) rejects negative value

    # Record must be unchanged (still 1000, not -1)
    records = store.load_all()
    assert records[0].token_count == 1000


def test_annotate_empty_id_no_options_reports_id_error(tmp_path: Path):
    """annotate with empty session_id and no field options gives session_id error, not no-op error.

    Guard ordering: session_id is validated before the nothing_supplied check,
    so the more precise error message is shown regardless of what options were passed.
    """
    import sys

    sessions_dir = tmp_path / "sessions"

    sys.argv = [
        "gptme-sessions",
        "--sessions-dir",
        str(sessions_dir),
        "annotate",
        "",  # empty session_id
        # no field options supplied either
    ]
    from click.testing import CliRunner
    from gptme_sessions.cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--sessions-dir", str(sessions_dir), "annotate", ""],
    )
    assert result.exit_code != 0
    assert "Session ID must not be empty" in result.output


# -- _count_unsynced and default view unsync hint ----------------------------


def test_count_unsynced_returns_zero_when_all_synced(tmp_path: Path, monkeypatch):
    """_count_unsynced returns 0 when all discovered sessions are already in the store."""
    from gptme_sessions.cli import _count_unsynced

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    store = SessionStore(sessions_dir=tmp_path / "sessions")
    # Simulate an already-synced record (journal_path = trajectory path)
    store.append(SessionRecord(harness="gptme", journal_path=str(fake_file)))

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    assert _count_unsynced(store) == 0


def test_count_unsynced_returns_count_of_new_sessions(tmp_path: Path, monkeypatch):
    """_count_unsynced counts discovered sessions not yet in the store."""
    from gptme_sessions.cli import _count_unsynced

    new_file = tmp_path / "new_session.jsonl"
    new_file.touch()

    store = SessionStore(sessions_dir=tmp_path / "sessions")
    # Store is empty — the discovered file has not been synced yet

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [new_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    assert _count_unsynced(store) == 1


def test_default_view_shows_unsync_count_when_pending(tmp_path: Path, capsys, monkeypatch):
    """Default gptme-sessions view shows unsync count when sessions are pending import."""
    import sys
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(harness="gptme", outcome="productive"))

    new_file = tmp_path / "new_session.jsonl"
    new_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [new_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir)])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "1 new session(s) available" in captured.out
    assert "gptme-sessions sync" in captured.out


def test_default_view_shows_generic_hint_when_fully_synced(tmp_path: Path, capsys, monkeypatch):
    """Default view shows generic sync hint when all discovered sessions are already in store."""
    import sys
    from gptme_sessions.cli import main

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(SessionRecord(harness="gptme", outcome="productive"))

    # No new sessions discovered
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    monkeypatch.setattr(sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir)])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "new session(s) available" not in captured.out
    assert "gptme-sessions sync" in captured.out


# -- sync model extraction + signals backfill ---------------------------------


def test_sync_captures_gptme_model_from_config(tmp_path: Path, capsys, monkeypatch):
    """sync reads model from gptme session config.toml and stores it in the record."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    # Create a fake gptme session directory with a config.toml declaring the model
    session_dir = tmp_path / "2026-03-07-test-session"
    session_dir.mkdir()
    jsonl = session_dir / "conversation.jsonl"
    jsonl.touch()
    (session_dir / "config.toml").write_text('[chat]\nmodel = "claude-sonnet-4-6"\n')

    monkeypatch.setattr(
        "gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [session_dir]
    )
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    rc = main()
    assert rc == 0

    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    assert len(records) == 1
    assert records[0].model == "claude-sonnet-4-6"
    assert records[0].model_normalized == "sonnet"


def test_sync_signals_backfills_existing_records(tmp_path: Path, capsys, monkeypatch):
    """sync --signals updates existing records that have outcome=unknown."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"

    # First sync — import without signals (outcome stays "unknown")
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    main()

    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    assert len(records) == 1
    assert records[0].outcome == "unknown"

    # Mock extract_from_path to return a productive result
    monkeypatch.setattr(
        "gptme_sessions.cli.extract_from_path",
        lambda p: {
            "productive": True,
            "session_duration_s": 300,
            "deliverables": ["abc123"],
            "inferred_category": "code",
        },
    )

    # Second sync with --signals — should update the existing record
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--signals"],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "updated 1" in captured.out

    records = store.load_all()
    assert len(records) == 1
    assert records[0].outcome == "productive"
    assert records[0].duration_seconds == 300
    assert records[0].category == "code"


def test_sync_signals_does_not_overwrite_existing_deliverables(tmp_path: Path, capsys, monkeypatch):
    """sync --signals preserves existing deliverables, consistent with category guard."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"

    # First sync — import without signals (outcome stays "unknown")
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    main()

    # Manually set deliverables on the stored record (simulating prior post-session annotation)
    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    assert len(records) == 1
    records[0].deliverables = ["prior-deliverable"]
    store.rewrite(records)

    # Mock extract_from_path returning different deliverables
    monkeypatch.setattr(
        "gptme_sessions.cli.extract_from_path",
        lambda p: {
            "productive": True,
            "session_duration_s": 120,
            "deliverables": ["new-deliverable"],
            "inferred_category": "code",
        },
    )

    # Backfill with --signals — deliverables should NOT be overwritten
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--signals"],
    )
    main()

    updated_records = store.load_all()
    assert updated_records[0].deliverables == ["prior-deliverable"]


def test_sync_dry_run_signals_skips_extraction(tmp_path: Path, capsys, monkeypatch):
    """sync --dry-run --signals does NOT call extract_from_path (just previews)."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"

    # First sync — import without signals
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    main()

    extraction_called = []

    def fake_extract(p):
        extraction_called.append(p)
        return {
            "productive": True,
            "session_duration_s": 300,
            "deliverables": [],
            "inferred_category": "code",
        }

    monkeypatch.setattr("gptme_sessions.cli.extract_from_path", fake_extract)

    # dry-run --signals should preview update without calling extract_from_path
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--dry-run", "--signals"],
    )
    rc = main()
    assert rc == 0
    assert extraction_called == [], "extract_from_path should not be called in dry-run mode"
    captured = capsys.readouterr()
    assert "would update" in captured.out


def test_sync_dry_run_signals_skips_extraction_for_new_records(tmp_path: Path, capsys, monkeypatch):
    """sync --dry-run --signals does NOT call extract_from_path for new (unimported) records."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"
    # Store is EMPTY — session has never been imported.

    extraction_called = []

    def fake_extract(p):
        extraction_called.append(p)
        return {
            "productive": True,
            "session_duration_s": 300,
            "deliverables": [],
            "inferred_category": "code",
        }

    monkeypatch.setattr("gptme_sessions.cli.extract_from_path", fake_extract)

    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--dry-run", "--signals"],
    )
    rc = main()
    assert rc == 0
    assert (
        extraction_called == []
    ), "extract_from_path should not be called for new records in dry-run mode"
    captured = capsys.readouterr()
    assert "would import" in captured.out


def test_sync_backfills_model_for_unknown_records(tmp_path: Path, capsys, monkeypatch):
    """sync updates model field on existing records that still have model='unknown'."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    # Pre-populate store with a record that has model="unknown" and the session path
    existing = SessionRecord(harness="claude-code", model="unknown", trajectory_path=str(fake_file))
    store.append(existing)

    # discover_cc_sessions returns the same file; extract_cc_model will find a real model
    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.extract_cc_model", lambda p: "claude-opus-4-6")

    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    rc = main()
    assert rc == 0

    records = store.load_all()
    assert len(records) == 1
    assert records[0].model == "claude-opus-4-6"


def test_sync_signals_failure_does_not_double_count_skipped(tmp_path: Path, capsys, monkeypatch):
    """When model update succeeds but signals extraction fails, session counts as updated not skipped."""
    import sys

    from gptme_sessions.cli import main
    from gptme_sessions import SessionStore, SessionRecord

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    # Pre-populate with unknown model + unknown outcome → both updates attempted
    existing = SessionRecord(harness="claude-code", model="unknown", trajectory_path=str(fake_file))
    store.append(existing)

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])
    # Model extraction succeeds
    monkeypatch.setattr("gptme_sessions.cli.extract_cc_model", lambda p: "claude-opus-4-6")

    # But signals extraction fails
    def _raise_signals_error(p):
        raise RuntimeError("signals extraction error")

    monkeypatch.setattr("gptme_sessions.cli.extract_from_path", _raise_signals_error)

    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--signals"],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()

    # Session should be counted as updated (model was fixed), not skipped
    assert "updated 1" in captured.out

    # Model should have been updated despite signals failure
    records = store.load_all()
    assert len(records) == 1
    assert records[0].model == "claude-opus-4-6"


def test_from_dict_migrates_jsonl_journal_path_to_trajectory_path():
    """Legacy records with .jsonl journal_path are migrated to trajectory_path."""
    data = {"journal_path": "/home/user/.local/share/gptme/logs/session/conversation.jsonl"}
    r = SessionRecord.from_dict(data)
    assert r.trajectory_path == "/home/user/.local/share/gptme/logs/session/conversation.jsonl"
    assert r.journal_path is None


def test_from_dict_migrates_directory_journal_path_to_trajectory_path():
    """Legacy gptme sessions without conversation.jsonl store a directory in journal_path.

    These were synced before trajectory_path was introduced, so migration must
    handle the directory case (no .jsonl suffix) to prevent duplicate imports
    on the next sync run.
    """
    data = {"journal_path": "/home/user/.local/share/gptme/logs/2026-01-15-120000-session"}
    r = SessionRecord.from_dict(data)
    assert r.trajectory_path == "/home/user/.local/share/gptme/logs/2026-01-15-120000-session"
    assert r.journal_path is None


def test_from_dict_preserves_md_journal_path():
    """Human-written .md journal entries are NOT migrated to trajectory_path."""
    data = {"journal_path": "/home/user/bob/journal/2026-01-15/session.md"}
    r = SessionRecord.from_dict(data)
    assert r.journal_path == "/home/user/bob/journal/2026-01-15/session.md"
    assert r.trajectory_path is None


def test_from_dict_no_migration_when_trajectory_path_already_set():
    """When trajectory_path is already present, journal_path is not touched."""
    data = {
        "trajectory_path": "/some/other.jsonl",
        "journal_path": "/home/user/.local/share/gptme/logs/session",
    }
    r = SessionRecord.from_dict(data)
    assert r.trajectory_path == "/some/other.jsonl"
    assert r.journal_path == "/home/user/.local/share/gptme/logs/session"


def test_from_dict_no_migration_when_trajectory_path_is_null():
    """A new-style record with trajectory_path=null is NOT migrated.

    ``filtered.get("trajectory_path") is None`` would incorrectly fire for
    records where trajectory_path was written as JSON ``null``.  The guard
    should use ``"trajectory_path" not in filtered`` so that an intentionally-
    absent field triggers migration while an explicit null is left alone.
    """
    data = {
        "trajectory_path": None,
        "journal_path": "/home/user/.local/share/gptme/logs/session",
    }
    r = SessionRecord.from_dict(data)
    assert r.trajectory_path is None
    assert r.journal_path == "/home/user/.local/share/gptme/logs/session"


def test_sync_signals_warns_when_trajectory_missing(tmp_path: Path, capsys, monkeypatch):
    """sync --signals emits a warning when a stored record's trajectory file is gone."""
    import sys

    from gptme_sessions.cli import main

    fake_file = tmp_path / "session.jsonl"
    fake_file.touch()

    monkeypatch.setattr("gptme_sessions.cli.discover_gptme_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_cc_sessions", lambda *a, **kw: [fake_file])
    monkeypatch.setattr("gptme_sessions.cli.discover_codex_sessions", lambda *a, **kw: [])
    monkeypatch.setattr("gptme_sessions.cli.discover_copilot_sessions", lambda *a, **kw: [])

    sessions_dir = tmp_path / "sessions"

    # First sync — import without signals (outcome stays "unknown")
    monkeypatch.setattr(
        sys, "argv", ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync"]
    )
    main()

    # Delete the trajectory file to simulate a moved/deleted session
    fake_file.unlink()

    # sync --signals should warn about the missing trajectory
    monkeypatch.setattr(
        sys,
        "argv",
        ["gptme-sessions", "--sessions-dir", str(sessions_dir), "sync", "--signals"],
    )
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "trajectory not found" in captured.err
