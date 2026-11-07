"""Per-page systemd units rendered from each route's cadence."""

from __future__ import annotations

import pytest
from gptme_vitals.routes import (
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
)
from gptme_vitals.timers import (
    CALENDAR,
    TimerError,
    _quote,
    render_unit_pair,
    render_units,
)


def _route(slug: str, cadence: RefreshCadence, **kw: object) -> DashboardRoute:
    base: dict[str, object] = dict(
        slug=slug,
        label=slug,
        href=f"/{slug}/",
        kind=RouteKind.STATIC,
        section="s",
        cadence=cadence,
        description=slug,
        output_path=f"{slug}/index.html",
        summary_source=f"{slug}/summary.json",
    )
    base.update(kw)
    return DashboardRoute(**base)  # type: ignore[arg-type]


EXE = "/usr/bin/gptme-vitals"


def test_every_scheduled_cadence_has_a_calendar_and_the_rest_do_not() -> None:
    assert set(CALENDAR) == set(RefreshCadence) - {
        RefreshCadence.MANUAL,
        RefreshCadence.N_A,
    }


def test_pair_runs_build_for_exactly_that_page() -> None:
    pair = render_unit_pair(
        _route("cost", RefreshCadence.FIVE_MIN),
        executable=EXE,
        build_args=["--agent", "bob"],
    )
    assert pair.service_name == "gptme-vitals-cost.service"
    assert pair.timer_name == "gptme-vitals-cost.timer"
    assert (
        'ExecStart="/usr/bin/gptme-vitals" "build" "cost" "--agent" "bob"'
        in pair.service
    )
    assert "OnCalendar=*:0/5\n" in pair.timer
    assert "WantedBy=timers.target" in pair.timer
    assert "Persistent=true" in pair.timer
    assert "Type=oneshot" in pair.service


def test_unscheduled_routes_are_reported_not_given_units() -> None:
    registry = RouteRegistry(
        (
            _route("a", RefreshCadence.HOURLY),
            _route("m", RefreshCadence.MANUAL),
            _route("n", RefreshCadence.N_A),
            _route("noenv", RefreshCadence.HOURLY, summary_source=None),
        )
    )
    pairs, unscheduled = render_units(registry, executable=EXE, build_args=[])
    assert [p.slug for p in pairs] == ["a"]
    assert unscheduled == ["m", "n"]


@pytest.mark.parametrize("slug", ["Cost", "a_b", "../x", "a b", "-x", "a.service"])
def test_unsafe_slug_is_refused(slug: str) -> None:
    with pytest.raises(TimerError, match="not safe"):
        render_unit_pair(
            _route(slug, RefreshCadence.HOURLY), executable=EXE, build_args=[]
        )


@pytest.mark.parametrize("exe", ["gptme-vitals", "./gptme-vitals", ""])
def test_relative_executable_is_refused(exe: str) -> None:
    with pytest.raises(TimerError, match="absolute"):
        render_unit_pair(
            _route("a", RefreshCadence.HOURLY), executable=exe, build_args=[]
        )


def test_unit_prefix_is_validated() -> None:
    with pytest.raises(TimerError, match="prefix"):
        render_unit_pair(
            _route("a", RefreshCadence.HOURLY),
            executable=EXE,
            build_args=[],
            unit_prefix="../etc",
        )


def test_manual_cadence_cannot_be_rendered_directly() -> None:
    with pytest.raises(TimerError, match="no schedule"):
        render_unit_pair(
            _route("a", RefreshCadence.MANUAL), executable=EXE, build_args=[]
        )


@pytest.mark.parametrize(
    ("arg", "quoted"),
    [
        ("plain", '"plain"'),
        ("with space", '"with space"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("100%", '"100%%"'),
        ("$HOME", '"$$HOME"'),
        ("a\\b", '"a\\\\b"'),
    ],
)
def test_quote_follows_systemd_rules(arg: str, quoted: str) -> None:
    assert _quote(arg) == quoted


@pytest.mark.parametrize("arg", ["a\nExecStartPre=/bin/evil", "a\rb", "a\0b"])
def test_control_characters_cannot_inject_a_directive(arg: str) -> None:
    with pytest.raises(TimerError, match="control character"):
        render_unit_pair(
            _route("a", RefreshCadence.HOURLY), executable=EXE, build_args=[arg]
        )
