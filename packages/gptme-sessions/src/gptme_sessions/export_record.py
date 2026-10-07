"""Typed export record for completed session runs.

Both Calendar and Sheet projections consume a ``CompletedRunExport`` feed
keyed by the canonical ``run_id`` (= ``SessionRecord.session_id``).  The
record is the single-source contract between the SessionStore and any
downstream sink — no sink should re-derive identity from journal filenames,
lock timestamps, or PIDs.

Usage::

    from gptme_sessions.export_record import CompletedRunExport, RunExportFilter
    from gptme_sessions.store import SessionStore

    store = SessionStore.load()
    filt = RunExportFilter(min_duration_seconds=60, run_types=["autonomous", "project-monitoring"])
    exports = [CompletedRunExport.from_session_record(r, filter=filt) for r in store.records]
    eligible = [e for e in exports if e.eligible]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gptme_sessions.record import SessionRecord

# Canonical run-type labels understood by both Calendar and Sheet sinks.
# Extend here when new session categories emerge — never in the sinks.
KNOWN_RUN_TYPES: set[str] = {
    "autonomous",
    "project-monitoring",
    "email",
    "worker",
    "operator",
    "interactive",
}

# Trigger-string → canonical run_type mapping (covers common harness prefixes).
_TRIGGER_TO_RUN_TYPE: dict[str, str] = {
    "timer": "autonomous",
    "dispatch": "autonomous",
    "event": "project-monitoring",
    "monitoring": "project-monitoring",
    "email": "email",
    "worker": "worker",
    "manual": "operator",
    "interactive": "interactive",
    "spawn": "worker",
}


def _normalize_run_type(trigger: str | None, run_type_raw: str | None) -> str | None:
    """Return a canonical run_type label from ``trigger`` (preferred) or legacy ``run_type``."""
    # Try trigger first (more reliable, finer-grained)
    if trigger:
        tl = trigger.lower()
        for prefix, canonical in _TRIGGER_TO_RUN_TYPE.items():
            if tl.startswith(prefix):
                return canonical
    # Fall back to legacy run_type field
    if run_type_raw and run_type_raw not in ("unknown", ""):
        rtl = run_type_raw.lower().strip()
        # Strip common prefixes ("autonomous-run" → "autonomous")
        for known in sorted(KNOWN_RUN_TYPES, key=len, reverse=True):
            if rtl.startswith(known.replace("-", "")):
                return known
            if rtl.startswith(known):
                return known
        return rtl or None
    return None


@dataclass
class RunExportFilter:
    """Filter parameters applied when producing ``CompletedRunExport`` records."""

    min_duration_seconds: int = 0
    """Skip runs shorter than this threshold (0 = no minimum)."""

    run_types: list[str] | None = None
    """Only include runs whose canonical ``run_type`` appears in this list.
    ``None`` means all types pass."""

    def is_eligible(self, export: "CompletedRunExport") -> tuple[bool, str | None]:
        """Return (eligible, skip_reason).  ``skip_reason`` is None when eligible."""
        if self.min_duration_seconds > 0 and export.duration_seconds < self.min_duration_seconds:
            return False, f"duration {export.duration_seconds}s < min {self.min_duration_seconds}s"
        if self.run_types is not None and export.run_type not in self.run_types:
            return False, f"run_type {export.run_type!r} not in filter {self.run_types}"
        return True, None


@dataclass
class CompletedRunExport:
    """Export record for a single completed session run.

    All fields are derived from ``SessionRecord``; no journal scanning or
    lock-history inference is performed here.  Downstream sinks (Calendar,
    Sheet) should carry ``run_id`` as their stable identity key and treat
    ``session_label`` as display metadata only.
    """

    run_id: str
    """Canonical session identity — stable, globally unique, never reused.
    Equals ``SessionRecord.session_id``.  Calendar stores this as UID/private
    property; Sheet stores it in a dedicated identity column."""

    session_label: str | None
    """Short human-readable label (e.g. ``"9d7f"``).  Reusable across dates —
    NOT suitable as a deduplication key.  Display metadata only."""

    start_time: str | None
    """ISO 8601 session start timestamp."""

    end_time: str | None
    """ISO 8601 session end timestamp."""

    duration_seconds: int
    """Total wall-clock duration of the session in seconds."""

    run_type: str | None
    """Canonical run-type label (see ``KNOWN_RUN_TYPES``).  ``None`` when the
    type cannot be determined — sinks must handle this explicitly rather than
    silently dropping the run."""

    outcome: str
    """Session outcome string: ``"productive"``, ``"noop"``, ``"blocked"``,
    ``"unknown"``, etc."""

    journal_path: str | None
    """Absolute path to the human-written journal file, or ``None`` when no
    journal was found."""

    category: str | None
    """Session work category (e.g. ``"code"``, ``"cleanup"``, ``"research"``).
    ``None`` when not set on the record."""

    eligible: bool
    """Whether this run passes the export filter and should be forwarded to sinks."""

    skip_reason: str | None
    """Human-readable explanation of why this run was excluded.  ``None`` when
    ``eligible`` is ``True``."""

    extra: dict = field(default_factory=dict)
    """Pass-through metadata for sink-specific extensions (e.g. raw model name,
    parent_session_id).  Sinks may read but must not require any key here."""

    @classmethod
    def from_session_record(
        cls,
        record: "SessionRecord",
        *,
        filter: RunExportFilter | None = None,
    ) -> "CompletedRunExport":
        """Build a ``CompletedRunExport`` from a ``SessionRecord``.

        Args:
            record: The source session record.
            filter: Optional filter applied to compute ``eligible`` /
                ``skip_reason``.  When ``None``, all runs are eligible.
        """
        run_type = _normalize_run_type(record.trigger, record.run_type)

        export = cls(
            run_id=record.session_id,
            session_label=record.session_label,
            start_time=record.start_time,
            end_time=record.end_time,
            duration_seconds=record.duration_seconds or 0,
            run_type=run_type,
            outcome=record.outcome or "unknown",
            journal_path=record.journal_path,
            category=record.category,
            eligible=True,
            skip_reason=None,
            extra={
                "model": record.model,
                "parent_session_id": record.parent_session_id,
            },
        )

        if filter is not None:
            export.eligible, export.skip_reason = filter.is_eligible(export)

        return export

    def as_calendar_uid(self) -> str:
        """Return a stable Calendar UID derived from ``run_id``.

        Replaces the legacy ``sha256(timestamp + PID)`` approach.  Prefixed
        to avoid any accidental collision with existing legacy UIDs.
        """
        return f"bob-run-{self.run_id}"

    def as_sheet_row_key(self) -> str:
        """Return the stable Sheet identity column value (= ``run_id``)."""
        return self.run_id
