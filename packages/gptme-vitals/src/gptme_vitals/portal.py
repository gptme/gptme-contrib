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
from gptme_vitals.routes import RouteRegistry

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


def render_cards(registry: RouteRegistry, envelopes: Iterable[PageEnvelope]) -> str:
    """Portal cards for the registry's public routes, in registry order.

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
    return registry.render_portal_cards(meta_html=meta, preview_html=preview)


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
        f"{head_html}\n<style>\n{PORTAL_CSS}\n</style>\n</head><body>\n"
        f"{registry.render_shared_nav(registry.portal_href)}\n"
        f"{toolbar_html}\n<h1>{escape(title)}</h1>\n{stamp}\n"
        f'<div class="cards">\n{render_cards(registry, envelopes)}\n</div>\n'
        "</body></html>\n"
    )
