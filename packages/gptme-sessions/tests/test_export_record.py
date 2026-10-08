"""Tests for CompletedRunExport — the typed export record for session sinks."""

from __future__ import annotations

from pathlib import Path

import pytest

from gptme_sessions.export_record import (
    KNOWN_RUN_TYPES,
    CompletedRunExport,
    RunExportFilter,
    _normalize_run_type,
    build_export_feed,
)
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    session_id: str = "abcd1234",
    session_label: str | None = "abcd",
    start_time: str | None = "2026-10-07T10:00:00Z",
    end_time: str | None = "2026-10-07T10:30:00Z",
    duration_seconds: int = 1800,
    trigger: str | None = "timer",
    run_type: str | None = "autonomous",
    outcome: str = "productive",
    journal_path: str | None = "/home/bob/bob/journal/2026-10-07/autonomous-session-abcd.md",
    category: str | None = "code",
) -> SessionRecord:
    r = SessionRecord(session_id=session_id)
    r.session_label = session_label
    r.start_time = start_time
    r.end_time = end_time
    r.duration_seconds = duration_seconds
    r.trigger = trigger
    r.run_type = run_type
    r.outcome = outcome
    r.journal_path = journal_path
    r.category = category
    return r


# ---------------------------------------------------------------------------
# _normalize_run_type
# ---------------------------------------------------------------------------


def test_normalize_run_type_from_trigger() -> None:
    assert _normalize_run_type("timer", None) == "autonomous"
    assert _normalize_run_type("dispatch", None) == "autonomous"
    assert _normalize_run_type("event", None) == "project-monitoring"
    assert _normalize_run_type("email", None) == "email"
    assert _normalize_run_type("worker", None) == "worker"
    assert _normalize_run_type("manual", None) == "operator"


def test_normalize_run_type_fallback_to_legacy() -> None:
    # trigger absent — should fall back to legacy run_type
    assert _normalize_run_type(None, "autonomous") == "autonomous"
    assert _normalize_run_type(None, "worker") == "worker"


def test_normalize_run_type_unknown_returns_none() -> None:
    assert _normalize_run_type(None, "unknown") is None
    assert _normalize_run_type(None, "Unknown") is None
    assert _normalize_run_type(None, "UNKNOWN") is None
    assert _normalize_run_type(None, "") is None
    assert _normalize_run_type(None, None) is None


@pytest.mark.parametrize("run_type", ["workers", "interactives", "operators", "emailer"])
def test_unknown_prefix_lookalike_is_preserved(run_type: str) -> None:
    # Only a hyphen-delimited legacy suffix is stripped; a bare prefix
    # ("workers" vs "worker") must not be silently truncated to a known type.
    assert _normalize_run_type(None, run_type) == run_type
    assert _normalize_run_type(None, run_type) not in KNOWN_RUN_TYPES


@pytest.mark.parametrize(
    ("run_type", "expected"),
    [("autonomous-run", "autonomous"), ("worker-heartbeat", "worker")],
)
def test_legacy_hyphen_suffix_is_stripped(run_type: str, expected: str) -> None:
    assert _normalize_run_type(None, run_type) == expected


@pytest.mark.parametrize(
    ("run_type", "trigger", "expected"),
    [
        ("monitoring", "timer", "project-monitoring"),
        ("autonomous", "spawn", "autonomous"),
        ("worker", "timer", "worker"),
        ("email", "dispatch", "email"),
    ],
)
def test_recognized_explicit_run_type_is_authoritative(
    run_type: str, trigger: str, expected: str
) -> None:
    assert _normalize_run_type(trigger, run_type) == expected


@pytest.mark.parametrize("run_type", ["monitoring", "project-monitoring", "project_monitoring"])
def test_monitoring_aliases_are_canonical(run_type: str) -> None:
    assert _normalize_run_type(None, run_type) == "project-monitoring"


@pytest.mark.parametrize(
    ("run_type", "trigger", "expected"),
    [
        (None, "timer", "autonomous"),
        ("", "dispatch", "autonomous"),
        ("unknown", "spawn", "worker"),
    ],
)
def test_trigger_is_fallback_for_absent_or_unknown_type(
    run_type: str | None, trigger: str, expected: str
) -> None:
    assert _normalize_run_type(trigger, run_type) == expected


@pytest.mark.parametrize("trigger", ["emailer", "workers", "timers", "spawned"])
def test_trigger_prefix_lookalike_is_not_coerced(trigger: str) -> None:
    # A trigger that merely starts with a known prefix must fall through; with
    # no explicit type there is nothing to infer, so the result is None.
    assert _normalize_run_type(trigger, None) is None


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        ("timer-auto", "autonomous"),
        ("monitoring-event", "project-monitoring"),
        ("timer.auto", "autonomous"),
        ("event:github", "project-monitoring"),
        ("dispatch/thing", None),
    ],
)
def test_trigger_separator_boundary_matches(trigger: str, expected: str | None) -> None:
    assert _normalize_run_type(trigger, None) == expected


# ---------------------------------------------------------------------------
# CompletedRunExport.from_session_record
# ---------------------------------------------------------------------------


def test_basic_fields_populated() -> None:
    record = _make_record()
    export = CompletedRunExport.from_session_record(record)

    assert export.run_id == "abcd1234"
    assert export.session_label == "abcd"
    assert export.run_type == "autonomous"
    assert export.outcome == "productive"
    assert export.duration_seconds == 1800
    assert export.journal_path is not None
    assert export.eligible is True
    assert export.skip_reason is None


def test_run_without_journal() -> None:
    """A run with no journal path is still exported."""
    record = _make_record(journal_path=None)
    export = CompletedRunExport.from_session_record(record)

    assert export.journal_path is None
    assert export.eligible is True


def test_two_runs_same_label_are_independent() -> None:
    """Two runs sharing a session_label produce two distinct records, keyed by run_id."""
    r1 = _make_record(session_id="run1aaaa", session_label="abcd")
    r2 = _make_record(session_id="run2bbbb", session_label="abcd")

    e1 = CompletedRunExport.from_session_record(r1)
    e2 = CompletedRunExport.from_session_record(r2)

    # Both survive and are independently identifiable
    assert e1.run_id != e2.run_id
    assert e1.session_label == e2.session_label == "abcd"
    assert e1.as_calendar_uid() != e2.as_calendar_uid()
    assert e1.as_sheet_row_key() != e2.as_sheet_row_key()


# ---------------------------------------------------------------------------
# RunExportFilter
# ---------------------------------------------------------------------------


def test_filter_min_duration_skips_short_run() -> None:
    record = _make_record(duration_seconds=30)
    filt = RunExportFilter(min_duration_seconds=60)
    export = CompletedRunExport.from_session_record(record, filter=filt)

    assert export.eligible is False
    assert "duration" in (export.skip_reason or "")


def test_filter_min_duration_passes_long_run() -> None:
    record = _make_record(duration_seconds=120)
    filt = RunExportFilter(min_duration_seconds=60)
    export = CompletedRunExport.from_session_record(record, filter=filt)

    assert export.eligible is True


def test_filter_run_types_skips_excluded_type() -> None:
    record = _make_record(trigger="worker", run_type=None)
    filt = RunExportFilter(run_types=["autonomous", "project-monitoring"])
    export = CompletedRunExport.from_session_record(record, filter=filt)

    assert export.eligible is False
    assert "run_type" in (export.skip_reason or "")


def test_filter_run_types_passes_included_type() -> None:
    record = _make_record(trigger="dispatch", run_type=None)
    filt = RunExportFilter(run_types=["autonomous"])
    export = CompletedRunExport.from_session_record(record, filter=filt)

    assert export.eligible is True


def test_filter_none_means_all_eligible() -> None:
    record = _make_record(trigger="manual")
    export = CompletedRunExport.from_session_record(record, filter=None)
    assert export.eligible is True


# ---------------------------------------------------------------------------
# Sink identity helpers
# ---------------------------------------------------------------------------


def test_calendar_uid_is_prefixed_and_stable() -> None:
    export = CompletedRunExport.from_session_record(_make_record(session_id="abc123"))
    uid = export.as_calendar_uid()
    assert uid == "bob-run-abc123"
    # Calling again returns same value (deterministic)
    assert export.as_calendar_uid() == uid


def test_calendar_uid_differs_from_legacy_sha256_pattern() -> None:
    """The new UID does NOT look like a SHA-256 hex string."""
    export = CompletedRunExport.from_session_record(_make_record())
    uid = export.as_calendar_uid()
    assert uid.startswith("bob-run-")
    assert len(uid) < 64  # SHA-256 hex is 64 chars


def test_sheet_row_key_is_run_id() -> None:
    export = CompletedRunExport.from_session_record(_make_record(session_id="unique99"))
    assert export.as_sheet_row_key() == "unique99"


# ---------------------------------------------------------------------------
# All known run types are passable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_type", sorted(KNOWN_RUN_TYPES))
def test_all_known_run_types_survive_filter(run_type: str) -> None:
    """A filter that accepts all known run types lets each through."""
    record = _make_record(trigger=None, run_type=run_type)
    filt = RunExportFilter(run_types=list(KNOWN_RUN_TYPES))
    export = CompletedRunExport.from_session_record(record, filter=filt)
    assert export.eligible is True, f"{run_type} was unexpectedly excluded"


# ---------------------------------------------------------------------------
# build_export_feed
# ---------------------------------------------------------------------------


@pytest.fixture
def make_store(tmp_path: Path):
    """Return a factory that writes records to a tmp_path store (auto-cleaned)."""

    def _make(records: list[SessionRecord]) -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path)
        for r in records:
            store.append(r)
        return store

    return _make


def test_build_export_feed_returns_all_records(make_store) -> None:
    """build_export_feed returns one export per record."""
    r1 = _make_record(session_id="run1aaaa", session_label="abcd")
    r2 = _make_record(session_id="run2bbbb", session_label="efgh")
    store = make_store([r1, r2])
    exports = build_export_feed(store=store)
    assert len(exports) == 2
    run_ids = {e.run_id for e in exports}
    assert run_ids == {"run1aaaa", "run2bbbb"}


def test_build_export_feed_two_same_label_are_independent(make_store) -> None:
    """Two records with the same session_label produce two distinct exports."""
    r1 = _make_record(session_id="run1aaaa", session_label="abcd")
    r2 = _make_record(session_id="run2bbbb", session_label="abcd")  # same label
    store = make_store([r1, r2])
    exports = build_export_feed(store=store)
    assert len(exports) == 2
    assert exports[0].run_id != exports[1].run_id
    assert exports[0].session_label == exports[1].session_label == "abcd"


def test_build_export_feed_filter_applied(make_store) -> None:
    """RunExportFilter is applied per record; ineligible records are still returned."""
    r_short = _make_record(session_id="run1aaaa", duration_seconds=10)
    r_long = _make_record(session_id="run2bbbb", duration_seconds=3600)
    store = make_store([r_short, r_long])
    filt = RunExportFilter(min_duration_seconds=60)
    exports = build_export_feed(store=store, filter=filt)
    assert len(exports) == 2  # both returned
    eligible = [e for e in exports if e.eligible]
    assert len(eligible) == 1
    assert eligible[0].run_id == "run2bbbb"


def test_build_export_feed_dedup_keeps_newest_regardless_of_order() -> None:
    """Duplicate session_ids dedup to the NEWEST record, independent of load order."""
    older = _make_record(session_id="run1aaaa", outcome="noop")
    older.timestamp = "2026-10-07T08:00:00+00:00"
    newer = _make_record(session_id="run1aaaa", outcome="productive")
    newer.timestamp = "2026-10-07T09:00:00+00:00"
    # Insert newest first: even in reverse load order, the newer record wins.
    store = _make_store_with_records([newer, older])
    exports = build_export_feed(store=store)
    assert len(exports) == 1
    assert exports[0].outcome == "productive"


def test_build_export_feed_dedup_mixed_naive_and_missing_timestamps() -> None:
    """Sorting must not raise TypeError when a naive-timestamped record
    coexists with one lacking a timestamp (naive vs aware comparison)."""
    naive = _make_record(session_id="run1aaaa", outcome="productive")
    # no tz suffix → naive datetime after parse; future so it wins the dedup
    naive.timestamp = "2030-01-01T08:00:00"
    missing = _make_record(session_id="run1aaaa", outcome="noop")
    missing.timestamp = ""  # store.append backfills with now() → later, loses
    store = _make_store_with_records([missing, naive])
    exports = build_export_feed(store=store)
    assert len(exports) == 1
    assert exports[0].outcome == "productive"
