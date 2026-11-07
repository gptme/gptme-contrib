"""Derived core.json / headline.json: rollup, blind pages, privacy and conflicts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gptme_vitals.build import (
    CORE_SCHEMA,
    HEADLINE_SCHEMA,
    BuildError,
    derive_core,
    write_core_files,
)
from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.routes import (
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
)

NOW = "2026-11-07T20:00:00Z"


def _route(slug: str, **kw: object) -> DashboardRoute:
    base: dict[str, object] = dict(
        slug=slug,
        label=slug.title(),
        href=f"/{slug}/",
        kind=RouteKind.STATIC,
        section="dashboards",
        cadence=RefreshCadence.HOURLY,
        description=f"{slug} page",
        output_path=f"{slug}/index.html",
        summary_source=f"{slug}/summary.json",
        badge="LIVE",
    )
    base.update(kw)
    return DashboardRoute(**base)  # type: ignore[arg-type]


def _registry(*extra: DashboardRoute) -> RouteRegistry:
    return RouteRegistry(
        (
            _route("services", core_dimension="services", is_headline=True),
            _route("ci", core_dimension="services"),
            _route("sessions", core_dimension="activity"),
            _route("notes"),  # emits an envelope but feeds no dimension
            *extra,
        )
    )


def _env(
    slug: str,
    status: Status,
    *,
    core: dict | None = None,
    detail: dict | None = None,
) -> PageEnvelope:
    return PageEnvelope(
        slug=slug,
        status=status,
        headline=f"{slug} headline",
        generated_at=NOW,
        core=core,
        detail=detail,
    )


def _derive(envs: list[PageEnvelope], reg: RouteRegistry | None = None):
    return derive_core(reg or _registry(), envs, agent="bob", generated_at=NOW)


def test_rolls_up_per_dimension_with_blind_page_counts() -> None:
    core, _ = _derive(
        [
            _env("services", Status.HEALTHY),
            _env("ci", Status.DEGRADED),
            _env("sessions", Status.HEALTHY),
        ]
    )
    assert core["schema"] == CORE_SCHEMA and core["agent"] == "bob"
    assert core["services"] == {
        "status": "degraded",
        "pages_total": 2,
        "pages_unknown": 0,
    }
    assert core["activity"] == {
        "status": "healthy",
        "pages_total": 1,
        "pages_unknown": 0,
    }
    assert core["status"] == "degraded"


def test_missing_envelope_is_a_counted_blind_page_not_a_green_one() -> None:
    core, _ = _derive(
        [_env("services", Status.HEALTHY)]
    )  # ci and sessions never emitted
    assert core["services"] == {
        "status": "healthy",
        "pages_total": 2,
        "pages_unknown": 1,
    }
    # sessions is the dimension's only page and it is blind: unknown, not healthy.
    assert core["activity"] == {
        "status": "unknown",
        "pages_total": 1,
        "pages_unknown": 1,
    }


def test_all_blind_overall_status_is_unknown() -> None:
    core, _ = _derive([_env("services", Status.UNKNOWN)])
    assert core["status"] == "unknown"


def test_critical_page_is_not_dropped_by_the_rollup() -> None:
    """The motivating bug: roll_up_status('healthy','healthy','critical') -> healthy."""
    core, _ = _derive([_env("services", Status.DOWN), _env("ci", Status.HEALTHY)])
    assert core["services"]["status"] == "down"
    assert core["status"] == "down"


def test_dimension_carries_only_the_allowlisted_core_projection() -> None:
    envs = [
        _env(
            "services",
            Status.HEALTHY,
            core={"failed_units": 0},
            detail={"secret_hostname": "node7.internal", "token": "sk-canary"},
        ),
        _env("ci", Status.HEALTHY, core={"red_repos": 1}),
    ]
    core, headline = _derive(envs)
    assert core["services"] == {
        "failed_units": 0,
        "red_repos": 1,
        "status": "healthy",
        "pages_total": 2,
        "pages_unknown": 0,
    }
    blob = json.dumps([core, headline])
    assert "secret_hostname" not in blob and "sk-canary" not in blob


def test_page_cannot_overwrite_computed_dimension_fields() -> None:
    envs = [_env("services", Status.DOWN, core={"status": "healthy"})]
    with pytest.raises(BuildError, match="computed by the rollup"):
        _derive(envs)
    with pytest.raises(BuildError, match="computed by the rollup"):
        _derive([_env("services", Status.DOWN, core={"pages_unknown": 0})])


def test_core_key_collision_inside_a_dimension_raises() -> None:
    envs = [
        _env("services", Status.HEALTHY, core={"count": 1}),
        _env("ci", Status.HEALTHY, core={"count": 2}),
    ]
    with pytest.raises(BuildError, match="set by both"):
        _derive(envs)


def test_envelope_without_a_dimension_contributes_nothing() -> None:
    core, _ = _derive(
        [_env("services", Status.HEALTHY), _env("notes", Status.DOWN, core={"x": 1})]
    )
    assert core["status"] == "healthy"
    assert "notes" not in core and "x" not in json.dumps(core)


def test_unregistered_and_duplicate_envelopes_raise() -> None:
    with pytest.raises(BuildError, match="unregistered slug 'ghost'"):
        _derive([_env("services", Status.HEALTHY), _env("ghost", Status.HEALTHY)])
    with pytest.raises(BuildError, match="two envelopes"):
        _derive([_env("services", Status.HEALTHY), _env("services", Status.DOWN)])


def test_headline_comes_from_the_headline_route() -> None:
    _, headline = _derive([_env("services", Status.DEGRADED)])
    assert headline == {
        "schema": HEADLINE_SCHEMA,
        "agent": "bob",
        "title": "Services",
        "value": "services headline",
        "status": "degraded",
        "generated_at": NOW,
        "link": "/services/",
    }


def test_headline_page_that_emitted_nothing_raises() -> None:
    with pytest.raises(BuildError, match="headline.json would be undefined"):
        _derive([_env("ci", Status.HEALTHY)])


def test_registry_without_a_headline_route_raises() -> None:
    reg = RouteRegistry((_route("a", core_dimension="d"),))
    with pytest.raises(ValueError, match="no route sets is_headline"):
        derive_core(reg, [_env("a", Status.HEALTHY)], agent="bob")


def test_write_core_files_round_trips_and_leaves_no_temp_files(tmp_path: Path) -> None:
    core, headline = _derive([_env("services", Status.HEALTHY)])
    write_core_files(tmp_path / "vitals", core, headline)
    out = tmp_path / "vitals"
    assert json.loads((out / "core.json").read_text()) == core
    assert json.loads((out / "headline.json").read_text()) == headline
    assert sorted(p.name for p in out.iterdir()) == ["core.json", "headline.json"]
