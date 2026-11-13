"""The portal: one renderer for every page's envelope.

Bob's portal grew to 2,282 lines because each page invented its own summary
shape and so needed its own ``_extract_<page>_preview`` function. Every page now
emits the same :class:`PageEnvelope`, so a card is three envelope fields
(status, headline, lines) and nothing page-specific.

Only ``PUBLIC`` routes get a card, and only ``status``/``headline``/``lines`` are
read, so ``detail`` and ``core`` cannot reach the page even if a caller passes
unfiltered envelopes. Every envelope string is HTML-escaped: headlines and lines
come from collectors that read tickets, branch names and error messages.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.routes import NAV_CSS, RouteRegistry

#: Status -> badge class. Matches the classes ``theme.BASE_CSS`` colours.
BADGE_CLASS: dict[Status, str] = {
    Status.DOWN: "badge-red",
    Status.DEGRADED: "badge-yellow",
    Status.STALE: "badge-yellow",
    Status.HEALTHY: "badge-green",
    Status.UNKNOWN: "badge-unknown",
    Status.NO_DATA: "badge-unknown",
}

# Colours come only from token variables so light/dark both work.
PORTAL_CSS = """
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.card { display: block; padding: 12px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--tile-bg); color: var(--text); text-decoration: none; }
a.card:hover { border-color: var(--accent); }
.card-title { font-weight: 600; }
.card-desc, .card-lines { color: var(--dim); font-size: 12px; }
.card-lines { margin: 6px 0 0; padding: 0; list-style: none; }
.card-headline { margin: 6px 0; font-size: 18px; }
.portal, .page { max-width: 1100px; margin: 0 auto; padding: 0 16px; }
.card-group { margin: 0 0 24px; }
.card-group h2 { margin: 16px 0 8px; font-size: 13px; font-weight: 600; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.06em; }
.page-lines { margin: 12px 0; padding-left: 20px; }
""".strip()


def _meta_html(envelope: PageEnvelope | None) -> str:
    """The status badge and headline for one card; a blind page says so."""
    if envelope is None:
        return (
            '<div class="card-headline">no data</div>'
            f'<span class="badge {BADGE_CLASS[Status.NO_DATA]}">{Status.NO_DATA}</span> '
        )
    status = Status(envelope.status)
    return (
        f'<div class="card-headline">{escape(envelope.headline)}</div>'
        f'<span class="badge {BADGE_CLASS[status]}">{escape(str(status))}</span> '
    )


def _preview_html(envelope: PageEnvelope | None) -> str:
    if envelope is None or not envelope.lines:
        return ""
    items = "".join(f"<li>{escape(line)}</li>" for line in envelope.lines)
    return f'<ul class="card-lines">{items}</ul>'


def _card_fragments(
    registry: RouteRegistry, envelopes: Iterable[PageEnvelope]
) -> tuple[dict[str, str], dict[str, str]]:
    """Per-slug meta and preview fragments for the registry's public routes.

    A route with a ``summary_source`` but no envelope shows "no data" rather than
    looking healthy; a route with no ``summary_source`` (an external link, say)
    gets a plain card.
    """
    by_slug = {e.slug: e for e in envelopes}
    meta: dict[str, str] = {}
    preview: dict[str, str] = {}
    for route in registry.public():
        if route.summary_source is None:
            continue
        envelope = by_slug.get(route.slug)
        meta[route.slug] = _meta_html(envelope)
        preview[route.slug] = _preview_html(envelope)
    return meta, preview


def render_cards(registry: RouteRegistry, envelopes: Iterable[PageEnvelope]) -> str:
    """Portal cards for the registry's public routes, flat, in registry order."""
    meta, preview = _card_fragments(registry, envelopes)
    return registry.render_portal_cards(meta_html=meta, preview_html=preview)


def render_card_groups(
    registry: RouteRegistry, envelopes: Iterable[PageEnvelope]
) -> str:
    """Portal cards under one heading per nav group, in nav order."""
    meta, preview = _card_fragments(registry, envelopes)
    return registry.render_portal_groups(meta_html=meta, preview_html=preview)


#: First line of every generated envelope page: lets a rebuild tell its own file
#: from a page another producer owns.
ENVELOPE_PAGE_MARKER = "<!-- gptme-vitals:envelope-page -->"


def render_envelope_page(
    registry: RouteRegistry,
    slug: str,
    envelope: PageEnvelope | None,
    *,
    head_html: str = "",
    toolbar_html: str = "",
) -> str:
    """A minimal page for a route that has an envelope but no dedicated renderer.

    Shows status, headline and preview lines (the same three fields as a portal
    card) under the shared nav, so the nav and portal link never lead to a
    directory listing. Like the portal it reads only public envelope fields.
    """
    route = registry.get_by_slug(slug)
    if route is None or route.href is None:
        raise KeyError(f"no page route {slug!r}")
    lines: list[str]
    if envelope is None:
        status_html = (
            f'<span class="badge {BADGE_CLASS[Status.NO_DATA]}">{Status.NO_DATA}</span>'
        )
        headline, lines, stamp = "no data", [], ""
    else:
        status = Status(envelope.status)
        status_html = (
            f'<span class="badge {BADGE_CLASS[status]}">{escape(str(status))}</span>'
        )
        headline, lines = envelope.headline, list(envelope.lines)
        stamp = (
            f'<p class="card-desc">Updated {escape(envelope.generated_at)}</p>'
            if envelope.generated_at
            else ""
        )
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    body_lines = f'<ul class="page-lines">{items}</ul>' if items else ""
    return (
        "<!doctype html>\n"
        f"{ENVELOPE_PAGE_MARKER}\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{escape(route.label)}</title>\n"
        f"{head_html}\n<style>\n{NAV_CSS}\n{PORTAL_CSS}\n</style>\n</head><body>\n"
        f'<main class="page">\n{registry.render_shared_nav(route.href)}\n'
        f"{toolbar_html}\n<h1>{escape(route.label)}</h1>\n"
        f'<p class="card-desc">{escape(route.description)}</p>\n'
        f'<p class="card-headline">{escape(headline)} {status_html}</p>\n'
        f"{body_lines}\n{stamp}\n</main>\n</body></html>\n"
    )


def render_portal(
    registry: RouteRegistry,
    envelopes: Iterable[PageEnvelope],
    *,
    title: str,
    head_html: str = "",
    toolbar_html: str = "",
    generated_at: str = "",
) -> str:
    """A complete portal page.

    ``head_html`` and ``toolbar_html`` are trusted, pre-built fragments (pass
    ``theme.render_head_assets()`` and ``theme.render_theme_toggle()``); ``title``
    and everything derived from envelopes is escaped here.
    """
    stamp = (
        f'<p class="card-desc">Updated {escape(generated_at)}</p>'
        if generated_at
        else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{escape(title)}</title>\n"
        f"{head_html}\n<style>\n{NAV_CSS}\n{PORTAL_CSS}\n</style>\n</head><body>\n"
        f'<main class="portal">\n{registry.render_shared_nav(registry.portal_href)}\n'
        f"{toolbar_html}\n<h1>{escape(title)}</h1>\n{stamp}\n"
        f"{render_card_groups(registry, envelopes)}\n"
        "</main>\n</body></html>\n"
    )
