"""Tests for gptme-sessions package."""

import json
from pathlib import Path

from gptme_sessions import SessionRecord, SessionStore
from gptme_sessions.signals import (
    _detect_format,
    detect_format,
    extract_signals,
    extract_signals_cc,
    extract_usage_cc,
    extract_usage_gptme,
    grade_signals,
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
            "content": ("@shell(c0): {}\n" "@shell(c1): {}\n" "@shell(c2): {}\n"),
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


def test_extract_usage_gptme_usage_field():
    """Token counts in msg.usage (legacy) are extracted correctly."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "usage": {"input_tokens": 50, "output_tokens": 25, "cost": 0.001},
            "metadata": {"model": "anthropic/claude-sonnet-4-6"},
        }
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 25


def test_extract_usage_gptme_openai_naming():
    """OpenAI-style naming (prompt_tokens, completion_tokens) is supported."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            "metadata": {"model": "openai/gpt-4o"},
        }
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["model"] == "openai/gpt-4o"


def test_extract_usage_gptme_meta_usage():
    """Token counts in msg.metadata.usage (nested) are extracted correctly."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "metadata": {
                "model": "anthropic/claude-opus-4-6",
                "usage": {"input_tokens": 300, "output_tokens": 100, "cost": 0.01},
            },
        }
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 100
    assert abs(usage["cost"] - 0.01) < 1e-9


def test_extract_usage_gptme_empty():
    """Empty trajectory returns empty dict."""
    assert extract_usage_gptme([]) == {}


def test_extract_usage_gptme_zero_cost_not_falsy():
    """Zero cost (e.g. free/local model) is preserved, not skipped by or-chaining."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cost": 0.0},
            "metadata": {
                "model": "local/llama",
                # cost also present here with a non-zero value that should NOT win
                "cost": 9.99,
            },
        }
    ]
    usage = extract_usage_gptme(msgs)
    # cost=0.0 from usage should win over metadata.cost=9.99
    assert usage["cost"] == 0.0
    assert usage["input_tokens"] == 100


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


def test_extract_usage_gptme_cache_tokens():
    """Anthropic cache tokens are extracted and included in total_tokens."""
    msgs = [
        {
            "role": "assistant",
            "content": "turn 1",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 400,
                "cache_read_input_tokens": 0,
                "cost": 0.005,
            },
        },
        {
            "role": "assistant",
            "content": "turn 2",
            "metadata": {
                "model": "anthropic/claude-sonnet-4-6",
                "input_tokens": 10,
                "output_tokens": 30,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 400,
                "cost": 0.001,
            },
        },
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["input_tokens"] == 110
    assert usage["output_tokens"] == 80
    assert usage["cache_creation_tokens"] == 400
    assert usage["cache_read_tokens"] == 400
    # total_tokens includes all four fields
    assert usage["total_tokens"] == 110 + 80 + 400 + 400


def test_extract_usage_gptme_cache_tokens_meta_usage():
    """Cache tokens are extracted from msg.metadata.usage (nested format)."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "metadata": {
                "model": "anthropic/claude-opus-4-6",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 300,
                    "cache_read_input_tokens": 150,
                    "cost": 0.002,
                },
            },
        }
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["cache_creation_tokens"] == 300
    assert usage["cache_read_tokens"] == 150
    assert usage["total_tokens"] == 50 + 20 + 300 + 150


def test_extract_usage_gptme_no_cache_tokens_zero():
    """Cache token keys are always present, defaulting to 0 for non-Anthropic models."""
    msgs = [
        {
            "role": "assistant",
            "content": "test",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            "metadata": {"model": "openai/gpt-4o"},
        }
    ]
    usage = extract_usage_gptme(msgs)
    assert usage["cache_creation_tokens"] == 0
    assert usage["cache_read_tokens"] == 0
    assert usage["total_tokens"] == 140


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
