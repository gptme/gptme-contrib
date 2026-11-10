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


def test_relative_links_consult_the_registry(tmp_path: Path) -> None:
    """`../decisions/` must agree with `/decisions/`: dynamic routes have no file."""
    resolve = _registry().resolve_internal_target
    assert resolve("/decisions/", current_route="/tasks/", site_root=tmp_path) is None
    assert resolve("../decisions/", current_route="/tasks/", site_root=tmp_path) is None
    # A registered static route resolves through its own output_path.
    assert (
        resolve("../services/", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "services/index.html"
    )
    assert (
        resolve("../services/index.html", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "services/index.html"
    )


def test_registered_output_path_wins_over_the_href_convention(tmp_path: Path) -> None:
    odd = _route("odd", href="/odd/", output_path="elsewhere/page.html")
    reg = RouteRegistry((odd,))
    assert (
        reg.resolve_internal_target("/odd/", current_route="/", site_root=tmp_path)
        == tmp_path / "elsewhere/page.html"
    )
    assert (
        reg.resolve_internal_target(
            "elsewhere/page.html", current_route="/", site_root=tmp_path
        )
        == tmp_path / "elsewhere/page.html"
    )


def test_query_and_fragment_only_links_keep_the_current_file(tmp_path: Path) -> None:
    resolve = _registry().resolve_internal_target
    assert (
        resolve("?tab=1", current_route="/tasks/lessons/foo.html", site_root=tmp_path)
        == tmp_path / "tasks/lessons/foo.html"
    )
    assert (
        resolve("?tab=1", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "tasks/index.html"
    )
    assert resolve("#top", current_route="/tasks/", site_root=tmp_path) is None


def test_encoded_filenames_are_decoded_but_stay_contained(tmp_path: Path) -> None:
    resolve = _registry().resolve_internal_target
    assert (
        resolve("/tasks/my%20file.html", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "tasks/my file.html"
    )
    assert (
        resolve("my%20file.html", current_route="/tasks/", site_root=tmp_path)
        == tmp_path / "tasks/my file.html"
    )
    # An encoded dot-segment is normalised, so it cannot escape the site root.
    assert (
        resolve("%2e%2e/%2e%2e/x", current_route="/tasks/", site_root=tmp_path) is None
    )


def test_nav_portal_link_uses_the_registered_portal() -> None:
    portal = _route(
        "home", label="Home", href="/dashboard/", output_path="dashboard/index.html"
    )
    nav = RouteRegistry((_route("a"),), portal=portal, nav_groups=(("G", ("a",)),))
    first = _anchors(nav.render_shared_nav("/a/"))[0]
    assert first == {"class": "nav-portal", "href": "/dashboard/"}
    assert ">Home</a>" in nav.render_shared_nav("/a/")
    assert (
        _anchors(RouteRegistry((_route("a"),)).render_shared_nav("/"))[0]["href"] == "/"
    )


@pytest.mark.parametrize("current_route", ["/odd/", "/odd/?old=1"])
@pytest.mark.parametrize("href", ["child.html", "../sibling.html", "?tab=1"])
def test_relative_urls_use_current_url_not_output_path(
    tmp_path: Path, href: str, current_route: str
) -> None:
    reg = RouteRegistry((_route("odd", output_path="elsewhere/page.html"),))
    expected = {
        "child.html": "odd/child.html",
        "../sibling.html": "sibling.html",
        "?tab=1": "elsewhere/page.html",
    }
    assert (
        reg.resolve_internal_target(
            href, current_route=current_route, site_root=tmp_path
        )
        == tmp_path / expected[href]
    )


@pytest.mark.parametrize("kind", [RouteKind.STATIC, RouteKind.DYNAMIC])
def test_encoded_registered_urls_resolve_consistently(
    tmp_path: Path, kind: RouteKind
) -> None:
    route = _route(
        "encoded",
        href="/my%20page/",
        kind=kind,
        output_path="elsewhere/page.html" if kind is RouteKind.STATIC else None,
    )
    reg = RouteRegistry((route,))
    expected = tmp_path / "elsewhere/page.html" if kind is RouteKind.STATIC else None
    for href in ("/my%20page/", "/my page/", "../my%20page/"):
        assert (
            reg.resolve_internal_target(
                href, current_route="/tasks/", site_root=tmp_path
            )
            == expected
        )


def test_trailing_slash_does_not_match_registered_output_file(tmp_path: Path) -> None:
    assert (
        _registry().resolve_internal_target(
            "/tasks/index.html/", current_route="/", site_root=tmp_path
        )
        == tmp_path / "tasks/index.html/index.html"
    )


def test_private_portal_is_rejected() -> None:
    with pytest.raises(RouteRegistryError, match="portal must be public"):
        RouteRegistry((), portal=replace(PORTAL, visibility=Visibility.PRIVATE))


@pytest.mark.parametrize(
    "output_path", ["/outside.html", "../outside.html", "a/../../outside.html"]
)
def test_output_paths_must_be_contained_relative_paths(output_path: str) -> None:
    with pytest.raises(
        RouteRegistryError, match="output_path must be a contained relative path"
    ):
        RouteRegistry((_route("a", output_path=output_path),))


def test_encoded_href_aliases_are_duplicate_destinations() -> None:
    with pytest.raises(RouteRegistryError, match="duplicate href"):
        RouteRegistry(
            (
                _route("encoded", href="/my%20page/"),
                _route("literal", href="/my page/"),
            )
        )


def test_encoded_current_url_keeps_its_directory(tmp_path: Path) -> None:
    reg = RouteRegistry(
        (_route("encoded", href="/my%20page/", output_path="other.html"),)
    )
    assert reg.get_by_href("/my%20page/") is reg.get_by_href("/my page/")
    assert (
        reg.resolve_internal_target(
            "child.html", current_route="/my%20page/", site_root=tmp_path
        )
        == tmp_path / "my page/child.html"
    )
    assert (
        reg.resolve_internal_target(
            "?tab=1", current_route="/my%20page/", site_root=tmp_path
        )
        == tmp_path / "other.html"
    )


def test_external_url_encoding_does_not_collapse_distinct_queries() -> None:
    encoded = replace(GRAFANA, href="https://example.com/?filter=a%26b")
    literal = replace(GRAFANA, slug="other", href="https://example.com/?filter=a&b")
    reg = RouteRegistry((encoded, literal))
    assert reg.get_by_href(encoded.href) is encoded
    assert reg.get_by_href(literal.href) is literal


@pytest.mark.parametrize("kind", [RouteKind.STATIC, RouteKind.DYNAMIC])
@pytest.mark.parametrize(
    "href", ["/decisions/?state=open", "../decisions/?state=open", "?state=open"]
)
def test_query_bearing_routes_resolve_before_path_only_fallback(
    tmp_path: Path, kind: RouteKind, href: str
) -> None:
    route = _route(
        "filtered",
        href="/decisions/?state=open",
        kind=kind,
        output_path="filtered.html" if kind is RouteKind.STATIC else None,
    )
    fallback = _route("decisions", output_path="all.html")
    reg = RouteRegistry((fallback, route))
    current = "/decisions/?state=closed" if href.startswith("?") else "/tasks/"
    expected = tmp_path / "filtered.html" if kind is RouteKind.STATIC else None
    assert (
        reg.resolve_internal_target(href, current_route=current, site_root=tmp_path)
        == expected
    )
    assert (
        reg.resolve_internal_target(
            "/decisions/?state=other", current_route="/", site_root=tmp_path
        )
        == tmp_path / "all.html"
    )


@pytest.mark.parametrize(
    "encoded,other",
    [("/a%3Fb/", "/a?b/"), ("/a%23b/", "/a#b/"), ("/a%253Fb/", "/a%3Fb/")],
)
def test_encoded_path_delimiters_do_not_alias_url_components(
    tmp_path: Path, encoded: str, other: str
) -> None:
    first = _route("first", href=encoded, output_path="first.html")
    second = _route("second", href=other, output_path="second.html")
    reg = RouteRegistry((first, second))
    assert reg.get_by_href(encoded) is first
    assert reg.get_by_href(other) is second
    assert (
        reg.resolve_internal_target(encoded, current_route="/", site_root=tmp_path)
        == tmp_path / "first.html"
    )


def test_portal_can_own_headline_and_contribute_its_envelope() -> None:
    portal = replace(
        PORTAL,
        summary_source="summary.json",
        core_dimension="activity",
        is_headline=True,
    )
    reg = RouteRegistry((), portal=portal)
    assert reg.headline_route() is portal
    assert reg.envelope_routes() == (portal,)
    assert reg.core_dimensions() == {"activity": (portal,)}
    # Portal ownership does not turn it into its own card or grouped nav link.
    assert reg.public() == ()
    assert reg.navigable() == ()


def test_portal_and_page_cannot_both_own_headline() -> None:
    portal = replace(PORTAL, summary_source="summary.json", is_headline=True)
    page = _route("page", summary_source="page/summary.json", is_headline=True)
    with pytest.raises(RouteRegistryError, match="exactly one"):
        RouteRegistry((page,), portal=portal)


@pytest.mark.parametrize(
    "href,current,expected",
    [
        (".", "/reports/v1.2/", "reports/v1.2/index.html"),
        ("..", "/reports/v1.2/child/", "reports/v1.2/index.html"),
        ("%2e", "/reports/v1.2/", "reports/v1.2/index.html"),
    ],
)
def test_terminal_dot_segments_remain_directory_links(
    tmp_path: Path, href: str, current: str, expected: str
) -> None:
    assert (
        RouteRegistry(()).resolve_internal_target(
            href, current_route=current, site_root=tmp_path
        )
        == tmp_path / expected
    )


@pytest.mark.parametrize("href", ["/reports/../health/", "/reports/%2e%2e/health/"])
def test_registered_dot_segments_match_resolved_destination(
    tmp_path: Path, href: str
) -> None:
    route = _route("health", href=href, output_path="checks.html")
    reg = RouteRegistry((route,))
    assert reg.get_by_href("/health/") is route
    assert (
        reg.resolve_internal_target(href, current_route="/", site_root=tmp_path)
        == tmp_path / "checks.html"
    )
    with pytest.raises(RouteRegistryError, match="duplicate href"):
        RouteRegistry((route, _route("alias", href="/health/")))


def test_encoded_slash_in_current_url_does_not_change_browser_parent(
    tmp_path: Path,
) -> None:
    route = _route("health", output_path="checks.html")
    reg = RouteRegistry((route,))
    assert (
        reg.resolve_internal_target(
            "../health/", current_route="/reports%2F2026/", site_root=tmp_path
        )
        == tmp_path / "checks.html"
    )


@pytest.mark.parametrize("current", ["/%2Fetc/", "/%2f%2fetc/"])
@pytest.mark.parametrize("href", ["resolv.conf", "", "?tab=1"])
def test_decoded_base_cannot_escape_site_root(
    tmp_path: Path, current: str, href: str
) -> None:
    assert (
        RouteRegistry(()).resolve_internal_target(
            href, current_route=current, site_root=tmp_path
        )
        is None
    )


@pytest.mark.parametrize("kind", [RouteKind.STATIC, RouteKind.DYNAMIC])
@pytest.mark.parametrize("href", ["", "?", "?state=closed", "child.html"])
def test_empty_reference_inherits_query_but_explicit_reference_replaces_it(
    tmp_path: Path, kind: RouteKind, href: str
) -> None:
    filtered = _route(
        "filtered",
        href="/decisions/?state=open",
        kind=kind,
        output_path="filtered.html" if kind is RouteKind.STATIC else None,
    )
    reg = RouteRegistry((_route("decisions", output_path="all.html"), filtered))
    expected = {
        "": tmp_path / "filtered.html" if kind is RouteKind.STATIC else None,
        "?": tmp_path / "all.html",
        "?state=closed": tmp_path / "all.html",
        "child.html": tmp_path / "decisions/child.html",
    }
    assert (
        reg.resolve_internal_target(
            href, current_route="/decisions/?state=open", site_root=tmp_path
        )
        == expected[href]
    )


def test_registered_fragment_is_not_resource_identity(tmp_path: Path) -> None:
    route = _route(
        "report", href="/report/#latest", output_path="generated/report.html"
    )
    reg = RouteRegistry((route,))
    assert (
        reg.resolve_internal_target(route.href, current_route="/", site_root=tmp_path)
        == tmp_path / "generated/report.html"
    )
    assert reg.get_by_href("/report/#other") is route
    with pytest.raises(RouteRegistryError, match="duplicate href"):
        RouteRegistry((route, _route("alias", href="/report/")))


@pytest.mark.parametrize("kind", [RouteKind.STATIC, RouteKind.DYNAMIC])
@pytest.mark.parametrize(
    "href", ["%2e%2e/../health/", "../%2E%2E/health/", "../../health/"]
)
def test_mixed_encoded_parent_segments_resolve_before_cancellation(
    tmp_path: Path, kind: RouteKind, href: str
) -> None:
    health = _route(
        "health",
        kind=kind,
        output_path="checks.html" if kind is RouteKind.STATIC else None,
    )
    expected = tmp_path / "checks.html" if kind is RouteKind.STATIC else None
    assert (
        RouteRegistry((health,)).resolve_internal_target(
            href, current_route="/reports/2026/", site_root=tmp_path
        )
        == expected
    )


@pytest.mark.parametrize("kind", [RouteKind.STATIC, RouteKind.DYNAMIC])
@pytest.mark.parametrize(
    "href",
    [
        "services/",
        "../services/",
        "?tab=1",
        "#top",
        "//other.example/services/",
        "https://other.example/services/",
    ],
)
def test_internal_registration_requires_root_relative_url(
    kind: RouteKind, href: str
) -> None:
    route = _route(
        "services",
        href=href,
        kind=kind,
        output_path="services/index.html" if kind is RouteKind.STATIC else None,
    )
    with pytest.raises(RouteRegistryError, match="internal href must be root-relative"):
        RouteRegistry((route,))
    with pytest.raises(RouteRegistryError, match="internal href must be root-relative"):
        RouteRegistry((), portal=route)
