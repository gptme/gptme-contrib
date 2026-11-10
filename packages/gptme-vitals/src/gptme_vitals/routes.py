"""Route registry: the typed inventory every dashboard surface reads.

An agent registers its pages once, as ``DashboardRoute`` rows in a
``RouteRegistry``. The portal cards, the grouped navigation, the link crawler
and (in later slices) the per-page build timers are all derived from that one
table, so no surface keeps its own list of pages.

The registry is a *contract*, not a table: this module ships no routes. Each
agent's concrete pages stay in the agent's own repository and are handed to
``RouteRegistry`` at import time. Page-specific rendering (what a "Decisions"
page is, which tasks belong on it) is the agent's business too.

Three fields exist because of measured failure modes, see the README:

* ``visibility`` is *not* ``is_internal``. ``is_internal`` means "hosted by us,
  not an outbound link" and drives link crawling; using it as a publish gate
  would invert its meaning.
* ``core_dimension`` names the fixed ``core.json`` dimension a page contributes
  to, so the derived rollup never depends on a page's private ``detail``.
* ``is_headline`` marks the single page whose envelope owns ``headline.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit


class RouteRegistryError(ValueError):
    """The registered routes violate the registry contract."""


class RouteKind(StrEnum):
    """How a dashboard route is made available."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    EXTERNAL = "external"


class RefreshCadence(StrEnum):
    """Expected refresh cadence for the route."""

    FIVE_MIN = "5min"
    TEN_MIN = "10min"
    FIFTEEN_MIN = "15min"
    TWENTY_MIN = "20min"
    THIRTY_MIN = "30min"
    HOURLY = "hourly"
    MANUAL = "manual"
    N_A = "n/a"


class Visibility(StrEnum):
    """Whether a route's output may appear in the served (public) tree.

    ``PRIVATE`` routes are built into the private root only; they are neither
    linked from the public nav nor written under the HTTP root.
    """

    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class DashboardRoute:
    """One canonical destination displayed by the portal."""

    slug: str
    label: str
    href: str
    kind: RouteKind
    section: str
    cadence: RefreshCadence
    description: str
    output_path: str | None = None
    summary_source: str | None = None
    badge: str = ""
    visibility: Visibility = Visibility.PUBLIC
    core_dimension: str | None = None
    is_headline: bool = False

    @property
    def is_internal(self) -> bool:
        return self.kind is not RouteKind.EXTERNAL

    @property
    def is_external(self) -> bool:
        return self.kind is RouteKind.EXTERNAL

    @property
    def is_public(self) -> bool:
        return self.visibility is Visibility.PUBLIC

    def static_target(self, site_root: Path) -> Path | None:
        """Return the generated file backing this static route."""
        if self.kind is not RouteKind.STATIC or self.output_path is None:
            return None
        return site_root / self.output_path

    def validate(self) -> None:
        """Raise ``RouteRegistryError`` when the row is internally inconsistent."""
        if not (self.slug and self.label and self.href):
            raise RouteRegistryError(
                f"route {self.slug!r}: slug, label and href are required"
            )
        if self.kind is RouteKind.STATIC and not self.output_path:
            raise RouteRegistryError(
                f"route {self.slug!r}: static routes need output_path"
            )
        if self.output_path is not None:
            output = PurePosixPath(self.output_path)
            if output.is_absolute() or ".." in output.parts:
                raise RouteRegistryError(
                    f"route {self.slug!r}: output_path must be a contained relative path"
                )
        if self.kind is not RouteKind.STATIC and self.output_path is not None:
            raise RouteRegistryError(
                f"route {self.slug!r}: only static routes have an output_path"
            )
        if self.kind is RouteKind.EXTERNAL and not self.href.startswith(
            ("http://", "https://")
        ):
            raise RouteRegistryError(
                f"route {self.slug!r}: external href must be absolute"
            )
        if self.core_dimension is not None and self.summary_source is None:
            # A page with no envelope source has nothing to roll up; declaring a
            # dimension for it would make the dimension permanently blind.
            raise RouteRegistryError(
                f"route {self.slug!r}: core_dimension needs a summary_source"
            )
        if self.is_headline and self.summary_source is None:
            raise RouteRegistryError(
                f"route {self.slug!r}: is_headline needs a summary_source"
            )
        if self.visibility is Visibility.PRIVATE and self.kind is RouteKind.EXTERNAL:
            raise RouteRegistryError(
                f"route {self.slug!r}: an external link cannot be private"
            )


@dataclass(frozen=True, slots=True)
class RouteRegistry:
    """The validated set of routes one agent's dashboard serves.

    ``nav_groups`` is the portal's reading order: ``(label, (slug, ...))``.
    Every internal public route must appear in exactly one group so each page is
    one click from every other; unknown or duplicated slugs raise at
    construction, not when a page is rendered.
    """

    routes: tuple[DashboardRoute, ...]
    portal: DashboardRoute | None = None
    nav_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    _by_slug: dict[str, DashboardRoute] = field(init=False, repr=False, compare=False)
    _by_href: dict[str, DashboardRoute] = field(init=False, repr=False, compare=False)
    _by_output: dict[str, DashboardRoute] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.portal is not None and not self.portal.is_public:
            raise RouteRegistryError("portal must be public")
        by_slug: dict[str, DashboardRoute] = {}
        by_href: dict[str, DashboardRoute] = {}
        for route in (*self.routes, *((self.portal,) if self.portal else ())):
            route.validate()
            if route.slug in by_slug:
                raise RouteRegistryError(f"duplicate slug {route.slug!r}")
            decoded_href = _href_key(route.href)
            if decoded_href in by_href:
                raise RouteRegistryError(f"duplicate href {route.href!r}")
            by_slug[route.slug] = route
            by_href[decoded_href] = route
        headlines = [r.slug for r in by_slug.values() if r.is_headline]
        if len(headlines) > 1:
            raise RouteRegistryError(
                f"exactly one route may set is_headline, got {headlines}"
            )
        grouped = [slug for _, slugs in self.nav_groups for slug in slugs]
        if len(grouped) != len(set(grouped)):
            raise RouteRegistryError("a slug appears in more than one nav group")
        for slug in grouped:
            route = by_slug.get(slug)
            if route is None or route is self.portal:
                raise RouteRegistryError(f"nav group names unknown route {slug!r}")
            if not route.is_internal or not route.is_public:
                raise RouteRegistryError(
                    f"nav group names {slug!r}, which is external or private"
                )
        if self.nav_groups:
            missing = [r.slug for r in self.navigable() if r.slug not in set(grouped)]
            if missing:
                raise RouteRegistryError(f"routes missing from nav groups: {missing}")
        object.__setattr__(self, "_by_slug", by_slug)
        object.__setattr__(self, "_by_href", by_href)
        object.__setattr__(
            self,
            "_by_output",
            {
                r.output_path: r
                for r in (*self.routes, *((self.portal,) if self.portal else ()))
                if r.output_path
            },
        )

    # -- lookup -----------------------------------------------------------

    def get_by_slug(self, slug: str) -> DashboardRoute | None:
        return self._by_slug.get(slug)

    def get_by_href(self, href: str) -> DashboardRoute | None:
        return self._by_href.get(_href_key(href))

    def in_section(self, section: str) -> tuple[DashboardRoute, ...]:
        return tuple(r for r in self.routes if r.section == section)

    def internal(self) -> tuple[DashboardRoute, ...]:
        return tuple(r for r in self.routes if r.is_internal)

    def public(self) -> tuple[DashboardRoute, ...]:
        """Routes allowed in the served tree."""
        return tuple(r for r in self.routes if r.is_public)

    def navigable(self) -> tuple[DashboardRoute, ...]:
        """Internal public routes: the set the shared nav must reach."""
        return tuple(r for r in self.routes if r.is_internal and r.is_public)

    def envelope_routes(self) -> tuple[DashboardRoute, ...]:
        """Routes that emit a page envelope (declare a ``summary_source``)."""
        return tuple(r for r in self._by_slug.values() if r.summary_source is not None)

    # -- core.json derivation --------------------------------------------

    def core_dimensions(self) -> dict[str, tuple[DashboardRoute, ...]]:
        """Routes grouped by the ``core.json`` dimension they contribute to."""
        grouped: dict[str, list[DashboardRoute]] = {}
        for route in self._by_slug.values():
            if route.core_dimension is not None:
                grouped.setdefault(route.core_dimension, []).append(route)
        return {name: tuple(rows) for name, rows in grouped.items()}

    def headline_route(self) -> DashboardRoute:
        """The route that owns ``headline.json``; raises unless exactly one."""
        for route in self._by_slug.values():
            if route.is_headline:
                return route
        raise RouteRegistryError(
            "no route sets is_headline; headline.json is undefined"
        )

    # -- link crawling ----------------------------------------------------

    def resolve_internal_target(
        self, href: str, *, current_route: str, site_root: Path
    ) -> Path | None:
        """Resolve a generated-site href to a file, or ``None`` when not crawlable."""
        parsed = urlsplit(href)
        if (
            parsed.scheme
            or parsed.netloc
            or href.startswith(("#", "mailto:", "javascript:"))
        ):
            return None

        # Resolve against the browser URL, never the backing output_path: a
        # registered /odd/ may be built as elsewhere/page.html. Strip the query
        # and fragment before converting the URL to a path.
        current = urlsplit(current_route)
        raw_current = current.path.lstrip("/")
        current_directory = not raw_current or raw_current.endswith("/")
        # Keep a directory URL's parent inside that directory rather than
        # climbing one level too far after PurePosixPath drops its trailing slash.
        current_path = PurePosixPath(
            raw_current + "index.html" if current_directory else raw_current
        )
        # Decode first: `%20` is a space on disk, and `%2e%2e` must be normalised
        # (and contained) like a literal `..`, not slip past the dot-segment check.
        raw_path = unquote(parsed.path)
        if not raw_path:
            # Query-only link (`?tab=1`): the target is the current page itself.
            relative = PurePosixPath(unquote(raw_current))
        elif raw_path.startswith("/"):
            relative = PurePosixPath(raw_path.lstrip("/"))
        else:
            # Choose the browser's parent before decoding: %2F is not a URL
            # separator and must not introduce another directory to climb.
            url_path = _normalize_relative_path(current_path.parent / parsed.path)
            if url_path is None:
                return None
            relative = PurePosixPath(unquote(str(url_path)))

        normalized = _normalize_relative_path(relative)
        if normalized is None:
            return None
        # Resolve the destination URL first and consult the registry, so a
        # relative link to a dynamic or differently-pathed route gets the same
        # answer as the root-relative spelling of it.
        directory = _is_directory_url(raw_path) if raw_path else current_directory
        # An empty reference inherits the base query; an explicit '?' clears it.
        query = (
            current.query
            if not parsed.path and "?" not in href.split("#", 1)[0]
            else parsed.query
        )
        route = self._route_for_path(normalized, directory=directory, query=query)
        if route is not None:
            return route.static_target(site_root)
        if directory or not normalized.suffix:
            normalized /= "index.html"
        return site_root / normalized

    def _route_for_path(
        self, path: PurePosixPath, *, directory: bool, query: str
    ) -> DashboardRoute | None:
        """The registered route a normalised site-relative path refers to."""
        text = "" if path == PurePosixPath(".") else path.as_posix()
        # Prefer an exact query-bearing registration; ordinary static pages
        # remain crawlable with arbitrary UI query parameters.
        for suffix in (f"?{query}", "") if query else ("",):
            for candidate in (f"/{text}", f"/{text}/" if text else "/"):
                route = self._by_href.get(quote(candidate, safe="/") + suffix)
                if route is not None and (candidate.endswith("/") or not directory):
                    return route
        return None if directory else self._by_output.get(text)

    # -- rendering --------------------------------------------------------

    def render_shared_nav(self, current_href: str) -> str:
        """Render Portal plus every navigable route, grouped, current page marked."""
        portal_href = escape(self.portal.href if self.portal else "/", quote=True)
        portal_label = escape(self.portal.label if self.portal else "Portal")
        links = [f'<a class="nav-portal" href="{portal_href}">{portal_label}</a>']
        for group_label, slugs in self.nav_groups:
            links.append('<span class="nav-sep" aria-hidden="true">|</span>')
            links.append(f'<span class="nav-group">{escape(group_label)}</span>')
            for slug in slugs:
                route = self._by_slug[slug]
                href = escape(route.href, quote=True)
                label = escape(route.label)
                if route.href == current_href:
                    links.append(
                        f'<a class="nav-current" aria-current="page" '
                        f'href="{href}">{label}</a>'
                    )
                else:
                    links.append(f'<a href="{href}">{label}</a>')
        return (
            '<nav class="vitals-nav" aria-label="Dashboard navigation">'
            + "".join(links)
            + "</nav>"
        )

    def render_portal_cards(
        self,
        *,
        meta_html: dict[str, str] | None = None,
        preview_html: dict[str, str] | None = None,
    ) -> str:
        """Render one portal card per public route, in registry order.

        ``meta_html`` / ``preview_html`` are pre-escaped fragments keyed by
        slug; the portal builds them from page envelopes.
        """
        meta_html = meta_html or {}
        preview_html = preview_html or {}
        cards: list[str] = []
        for route in self.public():
            target = ' target="_blank" rel="noreferrer"' if route.is_external else ""
            badge_class = "badge-yellow" if route.is_external else "badge-green"
            cards.append(
                f'<a class="card" data-route="{escape(route.slug, quote=True)}" '
                f'href="{escape(route.href, quote=True)}"{target}>'
                f'<div class="card-title">{escape(route.label)}</div>'
                f'<div class="card-desc">{escape(route.description)}</div>'
                f"{meta_html.get(route.slug, '')}{preview_html.get(route.slug, '')}"
                f'<span class="badge {badge_class}">{escape(route.badge)}</span>'
                "</a>"
            )
        return "\n".join(cards)


def _href_key(href: str) -> str:
    """Decode only local URL paths; queries and external URLs keep their identity."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return href
    # Re-escape decoded path delimiters so a literal '?' or '#' cannot become
    # a query/fragment boundary, and '%' cannot trigger a second decode.
    path = unquote(parsed.path)
    if any(part in {".", ".."} for part in path.split("/")):
        normalized = _normalize_relative_path(PurePosixPath(path.lstrip("/")))
        if normalized is not None:
            text = "" if normalized == PurePosixPath(".") else normalized.as_posix()
            path = ("/" if path.startswith("/") else "") + text
            if text and _is_directory_url(unquote(parsed.path)):
                path += "/"
    return parsed._replace(path=quote(path, safe="/"), fragment="").geturl()


def _is_directory_url(path: str) -> bool:
    """Terminal dot segments denote a directory even without a trailing slash."""
    return path.endswith("/") or path.rsplit("/", 1)[-1] in {".", ".."}


def _normalize_relative_path(path: PurePosixPath) -> PurePosixPath | None:
    """Collapse dot segments while keeping the target inside the generated site."""
    # Percent-decoding a site-relative URL can introduce a filesystem root.
    if path.is_absolute():
        return None
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


NAV_CSS = """
.vitals-nav { display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  padding:8px 0; margin-bottom:16px; border-bottom:1px solid var(--border, #30363d); }
.vitals-nav a { color:var(--dim, #8b949e); font-size:12px; text-decoration:none;
  padding:4px 8px; border-radius:4px; }
.vitals-nav a:hover { color:var(--accent, #58a6ff); background:var(--tile-bg, #1d2433); }
.vitals-nav .nav-portal { color:var(--accent, #58a6ff); font-weight:600; }
.vitals-nav .nav-sep { color:var(--dim, #8b949e); font-size:11px; }
.vitals-nav .nav-group { color:var(--dim, #8b949e); font-size:10px; text-transform:uppercase;
  letter-spacing:0.06em; opacity:0.7; }
.vitals-nav .nav-current { color:var(--text, #e6e9ef); background:var(--tile-bg, #1d2433);
  font-weight:600; }
""".strip()
