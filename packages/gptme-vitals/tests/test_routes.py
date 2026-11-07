"""Route registry contract: validation, lookups, nav, cards and link resolution."""

from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import pytest
from gptme_vitals.routes import (
    NAV_CSS,
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
    RouteRegistryError,
    Visibility,
)


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
        badge="LIVE",
    )
    base.update(kw)
    return DashboardRoute(**base)  # type: ignore[arg-type]


PORTAL = _route("portal", href="/", output_path="index.html", section="root")
GRAFANA = _route(
    "grafana",
    href="https://grafana.example/d/x",
    kind=RouteKind.EXTERNAL,
    section="external",
    cadence=RefreshCadence.N_A,
    output_path=None,
)
ROUTES = (
    _route("tasks", summary_source="tasks/summary.json", core_dimension="attention"),
    _route(
        "services",
        summary_source="services/summary.json",
        core_dimension="services",
        is_headline=True,
    ),
    _route(
        "sessions", summary_source="sessions/summary.json", core_dimension="activity"
    ),
    _route(
        "decisions",
        kind=RouteKind.DYNAMIC,
        cadence=RefreshCadence.N_A,
        output_path=None,
    ),
    _route("secrets", visibility=Visibility.PRIVATE),
    GRAFANA,
)
GROUPS = (
    ("Work", ("tasks", "sessions", "decisions")),
    ("Ops", ("services",)),
)


def _registry() -> RouteRegistry:
    # `secrets` is private so it must be absent from nav groups.
    return RouteRegistry(ROUTES, portal=PORTAL, nav_groups=GROUPS)


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchors.append({k: v or "" for k, v in attrs})


def _anchors(html: str) -> list[dict[str, str]]:
    parser = _Anchors()
    parser.feed(html)
    return parser.anchors


def test_lookups_and_filters() -> None:
    reg = _registry()
    assert reg.get_by_slug("tasks") is reg.get_by_href("/tasks/")
    assert reg.get_by_slug("missing") is None
    assert reg.get_by_href("/") is PORTAL  # portal resolves by href, not in routes
    assert [r.slug for r in reg.in_section("external")] == ["grafana"]
    assert "grafana" not in {r.slug for r in reg.internal()}
    assert "secrets" not in {r.slug for r in reg.public()}
    assert "secrets" not in {r.slug for r in reg.navigable()}
    assert {r.slug for r in reg.envelope_routes()} == {"tasks", "services", "sessions"}


def test_visibility_is_independent_of_is_internal() -> None:
    """A private route is still internal: crawling and publishing are different questions."""
    secrets = _registry().get_by_slug("secrets")
    assert secrets is not None
    assert secrets.is_internal and not secrets.is_public
    grafana = _registry().get_by_slug("grafana")
    assert grafana is not None
    assert grafana.is_external and grafana.is_public


def test_core_dimensions_group_pages_by_declared_dimension() -> None:
    dims = _registry().core_dimensions()
    assert {k: [r.slug for r in v] for k, v in dims.items()} == {
        "attention": ["tasks"],
        "services": ["services"],
        "activity": ["sessions"],
    }


def test_headline_route_is_exactly_one() -> None:
    assert _registry().headline_route().slug == "services"
    no_headline = tuple(replace(r, is_headline=False) for r in ROUTES)
    reg = RouteRegistry(no_headline, portal=PORTAL, nav_groups=GROUPS)
    with pytest.raises(RouteRegistryError, match="no route sets is_headline"):
        reg.headline_route()
    two = (*ROUTES[:2], replace(ROUTES[2], is_headline=True), *ROUTES[3:])
    with pytest.raises(RouteRegistryError, match="exactly one"):
        RouteRegistry(two, portal=PORTAL, nav_groups=GROUPS)


@pytest.mark.parametrize(
    "bad,match",
    [
        (lambda: _route("a", output_path=None), "static routes need output_path"),
        (
            lambda: _route("a", kind=RouteKind.DYNAMIC, cadence=RefreshCadence.N_A),
            "only static routes have an output_path",
        ),
        (
            lambda: replace(GRAFANA, href="/relative/"),
            "external href must be absolute",
        ),
        (
            lambda: replace(GRAFANA, visibility=Visibility.PRIVATE),
            "external link cannot be private",
        ),
        (
            lambda: _route("a", core_dimension="x"),
            "core_dimension needs a summary_source",
        ),
        (lambda: _route("a", is_headline=True), "is_headline needs a summary_source"),
        (lambda: _route(""), "required"),
    ],
)
def test_invalid_rows_raise(bad: object, match: str) -> None:
    with pytest.raises(RouteRegistryError, match=match):
        RouteRegistry((bad(),))  # type: ignore[operator]


def test_duplicate_slug_and_href_raise() -> None:
    with pytest.raises(RouteRegistryError, match="duplicate slug"):
        RouteRegistry((_route("a"), _route("a", href="/other/")))
    with pytest.raises(RouteRegistryError, match="duplicate href"):
        RouteRegistry((_route("a"), _route("b", href="/a/")))
    with pytest.raises(RouteRegistryError, match="duplicate href"):
        RouteRegistry((_route("a", href="/"),), portal=PORTAL)


def test_nav_groups_are_checked_at_construction_not_render_time() -> None:
    with pytest.raises(RouteRegistryError, match="unknown route 'nope'"):
        RouteRegistry(ROUTES, portal=PORTAL, nav_groups=(("X", ("nope",)),))
    with pytest.raises(RouteRegistryError, match="more than one nav group"):
        RouteRegistry(
            ROUTES,
            portal=PORTAL,
            nav_groups=(("A", ("tasks",)), ("B", ("tasks",))),
        )
    with pytest.raises(RouteRegistryError, match="external or private"):
        RouteRegistry(ROUTES, portal=PORTAL, nav_groups=(*GROUPS, ("Z", ("grafana",))))
    with pytest.raises(RouteRegistryError, match="missing from nav groups"):
        RouteRegistry(ROUTES, portal=PORTAL, nav_groups=(("Work", ("tasks",)),))


def test_registry_without_nav_groups_is_allowed() -> None:
    reg = RouteRegistry((_route("a"),))
    assert [a["href"] for a in _anchors(reg.render_shared_nav("/a/"))] == ["/"]


def test_shared_nav_reaches_every_navigable_route_and_marks_current() -> None:
    reg = _registry()
    nav = reg.render_shared_nav("/services/")
    anchors = _anchors(nav)
    assert anchors[0] == {"class": "nav-portal", "href": "/"}
    hrefs = [a["href"] for a in anchors[1:]]
    assert sorted(hrefs) == sorted(r.href for r in reg.navigable())
    assert [a["href"] for a in anchors if a.get("aria-current") == "page"] == [
        "/services/"
    ]
    assert 'class="vitals-nav"' in nav
    assert ".vitals-nav" in NAV_CSS


def test_nav_escapes_labels_and_hrefs() -> None:
    evil = _route("x", label="<b>x</b>", href='/x"y/')
    nav = RouteRegistry((evil,), nav_groups=(("A&B", ("x",)),)).render_shared_nav("/")
    assert "<b>" not in nav
    assert "A&amp;B" in nav
    assert 'href="/x&quot;y/"' in nav


def test_portal_cards_cover_public_routes_in_order_and_skip_private() -> None:
    reg = _registry()
    anchors = _anchors(reg.render_portal_cards())
    assert [a["data-route"] for a in anchors] == [r.slug for r in reg.public()]
    assert "secrets" not in {a["data-route"] for a in anchors}
    external = next(a for a in anchors if a["data-route"] == "grafana")
    assert external["target"] == "_blank"
    assert external["rel"] == "noreferrer"


def test_portal_cards_insert_meta_and_preview() -> None:
    cards = _registry().render_portal_cards(
        meta_html={"tasks": '<div class="card-meta">Updated now</div>'},
        preview_html={"tasks": '<div class="card-preview">3 active</div>'},
    )
    assert "Updated now" in cards and "3 active" in cards


def test_static_target_uses_output_path(tmp_path: Path) -> None:
    route = _registry().get_by_slug("tasks")
    assert route is not None
    assert route.static_target(tmp_path) == tmp_path / "tasks/index.html"
    decisions = _registry().get_by_slug("decisions")
    assert decisions is not None and decisions.static_target(tmp_path) is None


def test_resolve_internal_target_manifest_and_relative_links(tmp_path: Path) -> None:
    reg = _registry()
    resolve = reg.resolve_internal_target
    assert (
        resolve("/services/", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "services/index.html"
    )
    assert (
        resolve("x.html", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "tasks/x.html"
    )
    assert (
        resolve("../index.html", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "index.html"
    )
    for external in ("https://example.com", "#frag", "mailto:a@b.c", "javascript:x()"):
        assert resolve(external, current_route="/tasks/", site_root=tmp_path) is None
    # Escaping the site root is not crawlable.
    assert resolve("../../x", current_route="/tasks/", site_root=tmp_path) is None


def test_resolve_treats_unlisted_directory_routes_as_directories(
    tmp_path: Path,
) -> None:
    """A detail page below a directory route resolves against that directory.

    Dropping the trailing slash before ``.parent`` climbed one level too far
    and reported false broken links for every generated detail page.
    """
    resolve = _registry().resolve_internal_target
    assert (
        resolve(
            "../../skills/ship/index.html",
            current_route="/tasks/skills/self-push/",
            site_root=tmp_path,
        )
        == tmp_path / "tasks/skills/ship/index.html"
    )
    assert (
        resolve(
            "../other.html",
            current_route="/tasks/lessons/foo.html",
            site_root=tmp_path,
        )
        == tmp_path / "tasks/other.html"
    )
