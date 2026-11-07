# gptme-vitals

Shared agent-dashboard skeleton for gptme agents — the skeleton, design base and
standard pages that every agent's dashboard otherwise rebuilds from scratch.

Tracking issue: [gptme/gptme-contrib#2060](https://github.com/gptme/gptme-contrib/issues/2060).
`gptme-dashboard` stays what it is in practice: the generator for the
gptme-contrib catalog on GitHub Pages.

## Status

Slice 1, in progress. This commit lands the piece the rest depends on: the
typed **page envelope**.

## The page envelope

Every dashboard page emits one `PageEnvelope`. That is what lets the portal have
a single renderer instead of one `_extract_<page>_preview()` per page, and what
lets `core.json` be *derived* from the pages rather than hand-built as a second
contract.

```python
from gptme_vitals import PageEnvelope, Status, roll_up_envelopes

env = PageEnvelope(
    slug="bottlenecks",
    status=Status.DEGRADED,              # unrecognised values raise here
    headline="2 binding constraints",
    lines=("erik-queue: 168 gated PRs",),  # <= 4 short strings; the card's body
    detail={"evidence": "..."},          # private root only
    core={"binding_count": 2},           # allowlisted public projection
)

env.to_public()    # served tree — `detail` is structurally absent
env.to_private()   # private root — includes `detail`
```

Three properties are deliberate, each closing a measured failure:

**The status is an enum, validated at construction.** `fleet_vitals` validates
the top-level `core.json` status but never the per-dimension one, and
`roll_up_status` silently skips anything outside `STATUS_ORDER`. A derived
`core.json` turns pages into dimensions, landing exactly on the unvalidated
layer — so `roll_up_status("healthy", "healthy", "critical", "attention")`
returns `"healthy"` and a page reporting `critical` disappears. Validation
belongs on the page, at emit. An *absent* status keeps being ignored; that is
the staleness layer's job.

**`detail` and `core` are separate fields, not one filtered payload.** There is
no code path from the private field to the public writer, so a page cannot
publish its payload by forgetting a filter. Same shape as
`metaproductivity/error_lifecycle_publication.py`, where the private snapshot
lives outside the HTTP root and the served projection is computed separately
with a different key set.

**A blind page is counted, never hidden.** `roll_up_envelopes` returns
`pages_total`/`pages_unknown` beside the status. A dimension whose pages are all
`unknown` is `unknown`, not `healthy`; a dimension of one healthy page and one
blind page stays `healthy` but says so. Missing evidence must not read as good
evidence. `fleet_vitals.roll_up_status` is left alone — its "healthy if nothing
usable" default is documented and load-bearing for existing agent emits.

## Status vocabulary

`down` · `degraded` · `stale` · `healthy` (rankable, worst-first, identical to
`fleet_vitals.STATUS_ORDER`) plus `unknown` · `no-data` (legal, but carry no
severity).

Deliberately closed. A page that means "this only applies sometimes" puts that
in `headline`/`lines`; a page that means "this is bad" picks a severity. Every
added value is one some other agent's rollup will skip.

## The route registry

`RouteRegistry` is the one table every surface reads: portal cards, grouped
nav, the link crawler and the per-page build. The package ships the contract,
not the pages — an agent registers its own `DashboardRoute` rows (Bob's
19-entry table stays in his repo) and hands them in at import time.

```python
from gptme_vitals import DashboardRoute, RefreshCadence, RouteKind, RouteRegistry

REGISTRY = RouteRegistry(
    routes=(
        DashboardRoute(
            "services", "Services", "/services/", RouteKind.STATIC, "dashboards",
            RefreshCadence.HOURLY, "Systemd health and CI", "services/index.html",
            summary_source="services/summary.json",
            core_dimension="services",   # the fixed core.json dimension it feeds
            is_headline=True,            # owns headline.json
        ),
    ),
    nav_groups=(("Ops", ("services",)),),
)
REGISTRY.render_shared_nav("/services/")
REGISTRY.core_dimensions()   # {"services": (<route>,)} — input to roll_up_envelopes
```

Violations raise `RouteRegistryError` at construction, not when a page renders:
duplicate slug/href (including percent-encoded href aliases), an absolute or
parent-traversing `output_path`, a non-root-relative internal `href`, a private
portal, a nav group naming an unknown,
external or private route, a navigable route missing from every group, more than
one `is_headline`, and a
`core_dimension`/`is_headline` on a route with no `summary_source` (nothing to
roll up, so the dimension would be permanently blind). `headline_route()`
raises on zero, which is what `build` will call. The separately registered portal
participates in envelope collection, core dimensions, and headline ownership,
but is not rendered as a card linking to itself.

`visibility` is deliberately **not** `is_internal`. `is_internal` means "hosted
by us, not an outbound link" and drives link crawling; a private page is still
internal. `PRIVATE` routes are skipped by the nav and portal cards and are
meant for the private root only. A registered portal must be public because it
is linked from every page. Internal registrations (including the portal) use
root-relative URLs such as `/services/`, so their nav/card links have the same
destination on every page. Relative links found by the crawler remain supported
and resolve against the current page's
URL, then map the destination through the registry to its `output_path`; a
custom backing path never changes the browser's URL base. An exact query-bearing
registration wins over a path-only route; otherwise UI query parameters fall
back to the path-only route. Percent-encoded path delimiters remain distinct
from actual query/fragment boundaries.

Not moved from Bob's `dashboard_routes.py`: his route table and the
Decisions/Review-queue ownership predicates (they encode his page split).
Nav classes are `vitals-nav` (was `bob-nav`).

## Theme

`tokens.css` is the one palette, shipped as a file: dark by default, light when
the OS prefers it, and either one pinned with `<html data-theme="light|dark">`.
A pinned dark theme beats the OS preference; a pinned light one needs no media
query. Pages read the variables (`--bg`, `--card-bg`, `--text`, `--accent`,
`--green`/`--yellow`/`--red`, `--badge-*-bg`, ...) and never declare their own
`:root`; Bob's workspace carried 13 independent palettes before this.

```python
from gptme_vitals import render_head_assets, render_theme_toggle

head = render_head_assets()   # <style> tokens + base page CSS, pre-paint theme script
toggle = render_theme_toggle()  # button cycling auto -> light -> dark, persisted
```

Tests enforce that every dark token has a light value, that the two light
blocks (manual and media-query) are identical, and that text, status colours
and badges meet WCAG AA (4.5:1) on every surface in both schemes. The light
accent is `#9c5204`, darker than a straight inversion would give, because the
first draft measured 4.46:1 on the tile surface.

## Next in slice 1

The portal and `build` emitting the derived `core.json`/`headline.json` pair.
