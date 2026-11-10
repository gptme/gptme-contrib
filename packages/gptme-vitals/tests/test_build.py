"""Derived core.json / headline.json: rollup, blind pages, privacy and conflicts."""

from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path

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
    RouteRegistryError,
    Visibility,
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
    return derive_core(
        reg or _registry(),
        envs,
        agent="bob",
        generated_at=NOW,
        base_url="https://bob.example/",
    )


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
        "link": "https://bob.example/services/",
        "trend": "flat",
    }


def test_headline_matches_fleet_contract_without_copying_private_detail() -> None:
    make_headline = run_path(
        str(Path(__file__).resolve().parents[3] / "scripts" / "fleet_vitals.py")
    )["make_headline"]
    env = PageEnvelope(
        slug="services",
        status=Status.DEGRADED,
        headline="service health",
        generated_at=NOW,
        lines=("27 services running", "not part of the fleet subtitle"),
        detail={"private": "must not reach the fleet"},
    )
    _, headline = derive_core(
        _registry(),
        [env],
        agent="bob",
        generated_at=NOW,
        base_url="https://bob.example/vitals/",
    )
    expected = make_headline(
        "bob",
        "Services",
        "service health",
        status="degraded",
        detail="27 services running",
        link="https://bob.example/services/",
        generated_at=NOW,
    )
    assert headline == expected


@pytest.mark.parametrize("base_url", [None, "", "/vitals/", "https://", "file:///tmp/"])
def test_relative_headline_link_needs_an_absolute_http_base(
    base_url: str | None,
) -> None:
    with pytest.raises(BuildError, match="absolute HTTP"):
        derive_core(
            _registry(),
            [_env("services", Status.HEALTHY)],
            agent="bob",
            base_url=base_url,
        )


@pytest.mark.parametrize(
    ("href", "kind", "base_url", "expected"),
    [
        (
            "/services/",
            RouteKind.STATIC,
            "https://bob.example/vitals/",
            "https://bob.example/services/",
        ),
        (
            "/vitals/",
            RouteKind.STATIC,
            "https://bob.example/",
            "https://bob.example/vitals/",
        ),
        (
            "https://bob.example/services/",
            RouteKind.EXTERNAL,
            None,
            "https://bob.example/services/",
        ),
    ],
)
def test_headline_links_resolve_on_the_producing_agent(
    href: str, kind: RouteKind, base_url: str | None, expected: str
) -> None:
    reg = RouteRegistry(
        (
            _route(
                "services",
                href=href,
                kind=kind,
                output_path="services/index.html" if kind is RouteKind.STATIC else None,
                is_headline=True,
            ),
        )
    )
    _, headline = derive_core(
        reg, [_env("services", Status.HEALTHY)], agent="bob", base_url=base_url
    )
    assert headline["link"] == expected
    assert headline["trend"] == "flat"
    assert "detail" not in headline


@pytest.mark.parametrize(
    "href", ["services/", "javascript:alert(1)", "file:///tmp/services"]
)
def test_invalid_internal_headline_link_is_refused_by_registry(href: str) -> None:
    with pytest.raises(RouteRegistryError, match="internal href must be root-relative"):
        RouteRegistry((_route("services", href=href, is_headline=True),))


@pytest.mark.parametrize("href", ["https://", "http://"])
@pytest.mark.parametrize(
    "base_url", [None, "https://bob.example/vitals/", "http://bob.example/vitals/"]
)
def test_hostless_external_headline_link_is_refused_by_build(
    href: str, base_url: str | None
) -> None:
    reg = RouteRegistry(
        (
            _route(
                "services",
                href=href,
                kind=RouteKind.EXTERNAL,
                output_path=None,
                is_headline=True,
            ),
        )
    )
    with pytest.raises(BuildError, match="absolute HTTP"):
        derive_core(
            reg,
            [_env("services", Status.HEALTHY)],
            agent="bob",
            base_url=base_url,
        )


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


def test_private_headline_route_is_refused() -> None:
    reg = RouteRegistry(
        (
            _route(
                "audit",
                core_dimension="attention",
                is_headline=True,
                visibility=Visibility.PRIVATE,
            ),
        )
    )
    with pytest.raises(BuildError, match="is private"):
        derive_core(reg, [_env("audit", Status.HEALTHY)], agent="bob")


@pytest.mark.parametrize("name", ["status", "schema", "agent", "generated_at"])
def test_dimension_named_after_a_root_field_is_refused(name: str) -> None:
    reg = RouteRegistry(
        (_route("a", core_dimension=name, is_headline=True),),
    )
    with pytest.raises(BuildError, match="collides with a core@1 root field"):
        derive_core(reg, [_env("a", Status.HEALTHY)], agent="bob")


def test_write_uses_unique_temp_files_and_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gptme_vitals.build as build

    core, headline = _derive([_env("services", Status.HEALTHY)])
    seen: list[str] = []
    real_replace = build.os.replace

    def spy(src: str, dst: object) -> None:
        seen.append(Path(src).name)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(build.os, "replace", spy)
    write_core_files(tmp_path, core, headline)
    write_core_files(tmp_path, core, headline)
    assert len(seen) == len(set(seen)) == 4, "temp names must never repeat"

    def boom(src: str, dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(build.os, "replace", boom)
    with pytest.raises(OSError, match="rename failed"):
        write_core_files(tmp_path, core, headline)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["core.json", "headline.json"]


def test_shared_core_files_are_readable_by_a_separate_server_user(
    tmp_path: Path,
) -> None:
    core, headline = _derive([_env("services", Status.HEALTHY)])
    for _ in range(2):  # both initial publication and replacement keep public modes
        write_core_files(tmp_path, core, headline)
        for name in ("core.json", "headline.json"):
            assert (tmp_path / name).stat().st_mode & 0o777 == 0o644
