"""The hand-built fleet envelope API remains usable from an installed package."""

import json

import pytest
from gptme_vitals.fleet import emit, make_core, make_headline, roll_up_status

NOW = "2026-11-26T16:00:00Z"


def test_hand_built_core_keeps_extra_dimensions_and_legacy_rollup():
    core = make_core(
        "example",
        activity={"status": "healthy", "sessions": 3},
        inference={"status": "degraded", "failures": 2},
        generated_at=NOW,
    )
    assert core == {
        "schema": "fleet-vitals/core@1",
        "agent": "example",
        "generated_at": NOW,
        "status": "degraded",
        "activity": {"status": "healthy", "sessions": 3},
        "inference": {"status": "degraded", "failures": 2},
    }
    # Existing producers rely on this default; derive_core overrides it for
    # all-blind page sets rather than changing the hand-built API.
    assert roll_up_status(None, "unknown", "no-data", "critical") == "healthy"
    assert make_core("example", status="unknown")["status"] == "unknown"


@pytest.mark.parametrize("factory", [make_core, make_headline])
def test_invalid_top_level_status_still_raises(factory):
    args = ("example", "Sessions", "3") if factory is make_headline else ("example",)
    with pytest.raises(ValueError, match="invalid status 'critical'"):
        factory(*args, status="critical")


def test_emit_round_trips_the_pair_and_removes_an_obsolete_headline(tmp_path):
    core = make_core("example", generated_at=NOW)
    headline = make_headline(
        "example",
        "Sessions",
        "3",
        detail="2 productive",
        link="https://example.test/sessions/",
        generated_at=NOW,
    )
    out = tmp_path / "vitals"
    emit(out, core, headline)
    assert json.loads((out / "core.json").read_text()) == core
    assert json.loads((out / "headline.json").read_text()) == {
        "schema": "fleet-vitals/headline@1",
        "agent": "example",
        "title": "Sessions",
        "value": "3",
        "status": "healthy",
        "generated_at": NOW,
        "trend": "flat",
        "detail": "2 productive",
        "link": "https://example.test/sessions/",
    }
    emit(out, core)
    assert not (out / "headline.json").exists()


def test_headline_can_omit_optional_fields():
    headline = make_headline("example", "Sessions", "3", trend=None)
    assert not {"trend", "detail", "link"} & headline.keys()
