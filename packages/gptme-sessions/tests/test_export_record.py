"""Tests for CompletedRunExport — the typed export record for session sinks."""

from __future__ import annotations

import pytest

from gptme_sessions.export_record import (
    KNOWN_RUN_TYPES,
    CompletedRunExport,
    RunExportFilter,
    _normalize_run_type,
)
from gptme_sessions.record import SessionRecord


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
    assert _normalize_run_type(None, "") is None
    assert _normalize_run_type(None, None) is None


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
    record = _make_record(trigger="worker")
    filt = RunExportFilter(run_types=["autonomous", "project-monitoring"])
    export = CompletedRunExport.from_session_record(record, filter=filt)

    assert export.eligible is False
    assert "run_type" in (export.skip_reason or "")


def test_filter_run_types_passes_included_type() -> None:
    record = _make_record(trigger="dispatch")
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
