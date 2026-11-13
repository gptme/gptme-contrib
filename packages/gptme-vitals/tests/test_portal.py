"""The portal renders any envelope through one code path, escaped and private-safe."""

from __future__ import annotations

import pytest
from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.portal import (
    BADGE_CLASS,
    render_card_groups,
    render_cards,
    render_envelope_page,
    render_portal,
)
from gptme_vitals.routes import (
    NAV_CSS,
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
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


PORTAL = _route("portal", href="/", output_path="index.html", summary_source=None)
REGISTRY = RouteRegistry(
    (
        _route("services"),
        _route("cost", visibility=Visibility.PRIVATE),
        _route(
            "docs",
            kind=RouteKind.EXTERNAL,
            href="https://example.com/docs",
            output_path=None,
            summary_source=None,
        ),
        _route("sessions"),
    ),
    portal=PORTAL,
)


def _env(slug: str, status: Status = Status.HEALTHY, **kw: object) -> PageEnvelope:
    return PageEnvelope(
        slug=slug,
        status=status,
        headline=f"{slug} headline",
        generated_at=NOW,
        **kw,  # type: ignore[arg-type]
    )


def test_every_status_has_a_badge_class() -> None:
    assert set(BADGE_CLASS) == set(Status)


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (Status.DOWN, "badge-red"),
        (Status.DEGRADED, "badge-yellow"),
        (Status.HEALTHY, "badge-green"),
    ],
)
def test_card_shows_status_headline_and_lines(status: Status, cls: str) -> None:
    html = render_cards(REGISTRY, [_env("services", status, lines=("a", "b"))])
    assert f'badge {cls}">{status}' in html
    assert "services headline" in html
    assert "<li>a</li><li>b</li>" in html


def test_page_without_envelope_says_no_data_not_healthy() -> None:
    html = render_cards(REGISTRY, [])
    assert "no data" in html
    assert "badge-green" in html  # only the plain external-link badge
    assert 'badge badge-unknown">no-data' in html


def test_private_route_gets_no_card_even_if_its_envelope_is_passed() -> None:
    html = render_cards(
        REGISTRY, [_env("cost", detail={"secret": "x"}, core={"k": 1}, lines=("leak",))]
    )
    assert 'data-route="cost"' not in html
    assert "leak" not in html
    assert "cost headline" not in html


def test_detail_and_core_never_reach_the_page() -> None:
    html = render_cards(
        REGISTRY,
        [_env("services", detail={"token": "tok-123"}, core={"calls": 987654})],
    )
    assert "tok-123" not in html
    assert "987654" not in html


def test_external_link_card_is_plain() -> None:
    html = render_cards(REGISTRY, [])
    card = html.split('data-route="docs"')[1].split("</a>")[0]
    assert "card-headline" not in card
    assert "no data" not in card


PAYLOAD = '<script>alert(1)</script>"&'


def test_envelope_strings_are_escaped() -> None:
    env = PageEnvelope(
        slug="services", status=Status.DEGRADED, headline=PAYLOAD, lines=(PAYLOAD,)
    )
    html = render_portal(REGISTRY, [env], title="T")
    assert "<script>alert(1)" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 2


def test_title_and_timestamp_are_escaped() -> None:
    html = render_portal(REGISTRY, [], title=PAYLOAD, generated_at=PAYLOAD)
    assert "<script>alert(1)" not in html


def test_page_has_nav_title_cards_and_trusted_fragments() -> None:
    html = render_portal(
        REGISTRY,
        [_env("services")],
        title="Bob",
        head_html="<style>/*head*/</style>",
        toolbar_html="<button>t</button>",
        generated_at=NOW,
    )
    assert html.startswith("<!doctype html>")
    assert "<title>Bob</title>" in html
    assert "/*head*/" in html and "<button>t</button>" in html
    assert 'class="vitals-nav"' in html
    assert f"Updated {NOW}" in html
    assert html.index('data-route="services"') < html.index('data-route="sessions"')


def test_portal_ships_the_css_for_the_nav_it_renders() -> None:
    """The nav markup is useless without its stylesheet: links ran together as
    ``ServicesCI & PRs|MetricsSessions`` on Bob's live portal."""
    html = render_portal(REGISTRY, [_env("services")], title="Bob")
    assert 'class="vitals-nav"' in html
    assert NAV_CSS in html


GROUPED = RouteRegistry(
    (
        _route("services"),
        _route("sessions"),
        _route(
            "docs",
            kind=RouteKind.EXTERNAL,
            href="https://example.com/docs",
            output_path=None,
            summary_source=None,
        ),
        _route("fleet", kind=RouteKind.ENVELOPE, href=None, output_path=None),
    ),
    portal=PORTAL,
    nav_groups=(("Infra", ("services",)), ("Metrics", ("sessions",))),
)


def test_portal_cards_sit_under_group_headings_in_nav_order() -> None:
    html = render_card_groups(GROUPED, [])
    assert html.index("<h2>Infra</h2>") < html.index('data-route="services"')
    assert html.index('data-route="services"') < html.index("<h2>Metrics</h2>")
    assert html.index("<h2>Metrics</h2>") < html.index('data-route="sessions"')


def test_routes_outside_every_nav_group_share_an_other_section() -> None:
    html = render_card_groups(GROUPED, [])
    other = html[html.index("<h2>Other</h2>") :]
    assert 'data-route="docs"' in other and 'data-route="fleet"' in other
    assert 'data-route="services"' not in other


def test_registry_without_nav_groups_stays_one_ungrouped_grid() -> None:
    html = render_card_groups(REGISTRY, [])
    assert "<h2>" not in html
    assert html.count('class="cards"') == 1


def test_portal_page_is_a_centred_column_with_groups() -> None:
    html = render_portal(GROUPED, [], title="Bob")
    assert '<main class="portal">' in html and "max-width" in html
    assert "<h2>Infra</h2>" in html


def test_envelope_page_has_nav_status_and_escaped_lines() -> None:
    env = PageEnvelope(
        slug="services",
        status=Status.DEGRADED,
        headline=PAYLOAD,
        lines=(PAYLOAD, "second"),
        generated_at=NOW,
    )
    html = render_envelope_page(GROUPED, "services", env)
    assert 'class="vitals-nav"' in html and 'aria-current="page"' in html
    assert "badge-yellow" in html and "second" in html
    assert "<script>alert(1)" not in html


def test_envelope_page_without_data_says_so_and_unknown_slug_raises() -> None:
    html = render_envelope_page(GROUPED, "services", None)
    assert "no data" in html and "badge-unknown" in html
    with pytest.raises(KeyError):
        render_envelope_page(GROUPED, "fleet", None)
