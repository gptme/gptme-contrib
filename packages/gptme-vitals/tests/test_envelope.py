"""Tests for the gptme-vitals page envelope.

The interesting cases are not the constructor's happy path — they are the two
fail-open paths this contract exists to close, pinned here with the live
statuses that exposed them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from gptme_vitals.envelope import (
    ENVELOPE_SCHEMA,
    MAX_LINES,
    RANKABLE_STATUSES,
    PageEnvelope,
    Status,
    roll_up_envelopes,
)

# The 12 pages Bob's workspace served on 2026-11-07, which is both the adoption
# cost of this enum and its regression fixture. Five of the pages that raise
# report a severity the fleet view currently cannot see at all.
LIVE_PAGE_STATUSES: dict[str, str | None] = {
    "revenue": "healthy",
    "sessions": "healthy",
    "subs": "healthy",
    "services": "degraded",
    "bottlenecks": "critical",
    "actuation": "attention",
    "cost": "conditional",
    "machinery": "ok",
    "tasks": "ok",
    "actionable-health": None,
    "capacity": None,
    "release": None,
}
LEGAL_TODAY = {"revenue", "sessions", "subs", "services"}
RAISES_TODAY = {"bottlenecks", "actuation", "cost", "machinery", "tasks"}
ABSENT_TODAY = {"actionable-health", "capacity", "release"}


def _load_fleet_vitals():
    """Import ``scripts/fleet_vitals.py`` from the repo, or skip."""
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "fleet_vitals.py"
    if not path.exists():  # pragma: no cover - layout guard
        pytest.skip(f"fleet_vitals.py not found at {path}")
    spec = importlib.util.spec_from_file_location("_fleet_vitals_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _env(slug: str, status: str | Status) -> PageEnvelope:
    return PageEnvelope(slug=slug, status=Status(status), headline=f"{slug} headline")


# --- the contract itself -------------------------------------------------


def test_envelope_round_trips_through_public_payload():
    env = PageEnvelope(
        slug="bottlenecks",
        status=Status.DEGRADED,
        headline="2 binding constraints",
        lines=("erik-queue: 168 gated PRs", "release clicks: 4 pending"),
        core={"binding_count": 2},
    )
    payload = env.to_public()
    assert payload["schema"] == ENVELOPE_SCHEMA
    assert payload["status"] == "degraded"
    assert payload["lines"] == [
        "erik-queue: 168 gated PRs",
        "release clicks: 4 pending",
    ]
    assert payload["core"] == {"binding_count": 2}


def test_detail_is_structurally_absent_from_the_public_payload():
    env = PageEnvelope(
        slug="sessions",
        status=Status.HEALTHY,
        headline="41 sessions today",
        detail={"journal_excerpt": "private prose nobody outside may read"},
        core={"sessions_today": 41},
    )
    assert "detail" not in env.to_public()
    assert env.to_private()["detail"] == {
        "journal_excerpt": "private prose nobody outside may read"
    }


def test_lines_are_capped_so_a_card_cannot_become_a_page():
    with pytest.raises(ValueError, match="exceeds MAX_LINES"):
        PageEnvelope(
            slug="portal-polluter",
            status=Status.HEALTHY,
            headline="x",
            lines=tuple(f"line {i}" for i in range(MAX_LINES + 1)),
        )


def test_blank_slug_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        PageEnvelope(slug="   ", status=Status.HEALTHY, headline="x")


# --- fail-open path 1: an unrecognised status must raise, not be skipped ---


@pytest.mark.parametrize("slug", sorted(LEGAL_TODAY))
def test_live_pages_with_a_recognised_status_are_accepted(slug):
    raw = LIVE_PAGE_STATUSES[slug]
    assert raw is not None
    assert _env(slug, raw).is_rankable


@pytest.mark.parametrize("slug", sorted(RAISES_TODAY))
def test_live_pages_with_an_unrecognised_status_raise_at_construction(slug):
    raw = LIVE_PAGE_STATUSES[slug]
    assert raw is not None
    with pytest.raises(ValueError):
        _env(slug, raw)


@pytest.mark.parametrize("slug", sorted(RAISES_TODAY))
def test_an_untyped_caller_cannot_sneak_a_raw_status_past_the_enum(slug):
    """The type annotation stops checked callers; this is the runtime belt."""
    with pytest.raises(ValueError):
        PageEnvelope(
            slug=slug,
            status=LIVE_PAGE_STATUSES[slug],  # type: ignore[arg-type]
            headline="x",
        )


@pytest.mark.parametrize("slug", sorted(ABSENT_TODAY))
def test_live_pages_with_no_status_have_no_value_to_validate(slug):
    # An absent status stays the staleness layer's problem; it is not an error
    # here. The page must name one explicitly to be rolled up.
    assert LIVE_PAGE_STATUSES[slug] is None
    assert not _env(slug, Status.UNKNOWN).is_rankable


def test_critical_would_otherwise_be_dropped_by_the_upstream_rollup():
    """The reason the enum is validated at the page, not at ``make_core``."""
    fv = _load_fleet_vitals()
    assert fv.roll_up_status("healthy", "healthy", "critical", "attention") == "healthy"


def test_make_core_does_not_validate_per_dimension_status():
    """Characterization: the upstream raise exists, but not where it protects us."""
    fv = _load_fleet_vitals()
    with pytest.raises(ValueError):
        fv.make_core("bob", status="critical")
    core = fv.make_core(
        "bob",
        activity={"status": "critical"},
        spend={"status": "healthy"},
    )
    assert core["status"] == "healthy"  # the dropped severity, reproduced


# --- fail-open path 2: all-blind dimensions must not report green ----------


def test_all_unknown_pages_roll_up_to_unknown_not_healthy():
    fv = _load_fleet_vitals()
    assert fv.roll_up_status("unknown", "no-data") == "healthy"  # upstream default
    rollup = roll_up_envelopes([_env("a", Status.UNKNOWN), _env("b", Status.NO_DATA)])
    assert rollup.status is Status.UNKNOWN
    assert rollup.pages_total == 2
    assert rollup.pages_unknown == 2


def test_a_blind_page_beside_a_healthy_one_is_counted_not_hidden():
    rollup = roll_up_envelopes([_env("a", Status.HEALTHY), _env("b", Status.UNKNOWN)])
    assert rollup.status is Status.HEALTHY  # not noisy — the status does not move
    assert rollup.as_dimension() == {
        "status": "healthy",
        "pages_total": 2,
        "pages_unknown": 1,
    }


def test_rollup_is_worst_of_over_rankable_statuses():
    rollup = roll_up_envelopes(
        [
            _env("a", Status.HEALTHY),
            _env("b", Status.DEGRADED),
            _env("c", Status.STALE),
            _env("d", Status.UNKNOWN),
        ]
    )
    assert rollup.status is Status.DEGRADED
    assert (rollup.pages_total, rollup.pages_unknown) == (4, 1)


def test_empty_dimension_is_unknown():
    rollup = roll_up_envelopes([])
    assert rollup.status is Status.UNKNOWN
    assert rollup.as_dimension()["pages_total"] == 0


def test_as_dimension_accepts_the_pages_allowlisted_numbers():
    rollup = roll_up_envelopes([_env("a", Status.HEALTHY)])
    assert rollup.as_dimension(sessions_today=41)["sessions_today"] == 41


# --- the two vocabularies must not drift -----------------------------------


def test_status_vocabulary_matches_fleet_vitals():
    fv = _load_fleet_vitals()
    assert [str(s) for s in RANKABLE_STATUSES] == fv.STATUS_ORDER
    assert {str(s) for s in Status} == set(fv._VALID_STATUS)
