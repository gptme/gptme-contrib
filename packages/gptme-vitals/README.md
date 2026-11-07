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

## Derived `core.json` / `headline.json`

`derive_core(registry, envelopes, agent=..., base_url=...)` returns the `fleet-vitals/core@1`
and `headline@1` pair; `write_core_files()` replaces each file atomically with
mode 0644. This is not a two-file transaction: concurrent builders must serialize
the read/derive/write cycle, and readers can see different generations between
replacements. Nothing is hand-built: each dimension is the rollup of the pages
that declared it.

```python
from gptme_vitals import derive_core, write_core_files

core, headline = derive_core(
    REGISTRY, envelopes, agent="bob", base_url="https://bob.example/"
)
write_core_files("state/vitals", core, headline)
```

- A dimension is `{**allowlisted page core projections, status, pages_total,
  pages_unknown}`. The computed fields are written last and a page's `core`
  may not set them; two pages in one dimension may not share a key. `detail`
  never takes part, so the file is safe to pull across agents by construction.
- A route that declares a dimension but emitted no envelope is a **counted blind
  page**: it raises `pages_total` and `pages_unknown`, and a dimension whose
  pages are all blind is `unknown`, never `healthy`. The overall status is the
  worst rankable dimension, `unknown` if none is rankable.
- `headline.json` comes from the one `is_headline` route, which must be public
  (it copies the page's headline text, label and link into a file other agents
  pull). Zero headline routes raise `RouteRegistryError` during derivation and
  multiple raise it when the registry is constructed. A private headline route,
  a headline page that emitted nothing, or a dimension named after a `core@1`
  root field (`schema`, `agent`, `generated_at`, `status`) raises `BuildError`
  rather than producing an undefined or corrupt file.
- The shared headline link must be absolute HTTP(S): a relative route requires
  the producing agent's `base_url`, resolved with standard URL-join semantics
  (a leading `/` starts at the host root). Otherwise derivation raises
  `BuildError`, rather than making the fleet card link to the aggregator's host.
  `headline.detail` comes only from the public first `lines` entry when non-empty,
  never from private `PageEnvelope.detail`. `trend` is explicitly `"flat"`, the
  existing fleet-contract default; no trend is inferred from a single envelope.
- An envelope for a slug no route owns, or two for one slug, raises.

It does not import `fleet_vitals`; the two share only the schema strings and the
status vocabulary.

## Private and public roots

`publish_page` writes each envelope by construction, not by a filter:

| route visibility | private root | public root |
|---|---|---|
| `PUBLIC` | `to_private()` (with `detail`), mode 0600 | `to_public()` (no `detail`) |
| `PRIVATE` | `to_private()` | nothing |

The location under either root is the route's `summary_source`, which must be a
relative path with no `..`. The roots must not overlap (identical, nested either
way, or reached through a symlink): that would make the private root reachable
through the served tree, so it raises `PublishError` before anything is written.

`load_envelope` reads a stored envelope back and drops `detail`. A missing,
unparsable, wrongly-shaped or unrecognised-status file is a *blind* page (`None`),
which `derive_core` counts in `pages_unknown`; one corrupt page never stops the
others rebuilding.

## `gptme-vitals build [page...]`

```txt
gptme-vitals build [page...] --registry pkg.mod:REGISTRY --emitters pkg.mod:EMITTERS \
    --private-root DIR --public-root DIR --agent NAME
```

`REGISTRY` is a `RouteRegistry`; `EMITTERS` maps `slug -> () -> PageEnvelope`.
Naming pages rebuilds only those. `core.json` and `headline.json` are always
re-derived over **all** pages, the rest read back from the private root, so a
partial build never leaves the fleet view describing only what was just touched.
Reading from the private root (not the served one) means a private page feeding a
dimension counts the same whether or not it was rebuilt this run.

A failing emitter is reported, the other pages still publish, and the failed
page keeps its previous file. Exit status is 1 if any page or the derived files
failed, 2 for a usage error (unknown page, bad `module:ATTR`, overlapping roots).
A page that fails stays at its last published status; it does not turn `stale`
by itself, so schedule a freshness check on `generated_at` if that matters.

## Next in slice 1

The portal's single renderer and per-page systemd timers generated from each
route's `cadence`.
