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
portal or one that is not a hosted page, a nav group naming an unknown,
external, private or envelope-only route, a navigable route missing from every group, more than
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

### Envelope-only routes

Some pages are only an envelope: they feed a portal card and a `core.json`
dimension and have no HTML page of their own. Register them as
`RouteKind.ENVELOPE` with `href=None`, a `summary_source` and no `output_path`:

```python
DashboardRoute(
    "inference", "Inference", None, RouteKind.ENVELOPE, "dashboards",
    RefreshCadence.HOURLY, "Model routing and spend",
    summary_source="inference/summary.json", core_dimension="inference",
)
```

An envelope route is not `is_internal`: it is not crawled, not in `navigable()`
and may not be named in a nav group. Its portal card is a non-link `<div>` that
still shows the status, headline and lines. If it is the `is_headline` route,
`headline.json` links to the portal, where that card is. Declaring the same row
`STATIC` with an `output_path` nothing writes would put a dead link in the
portal, the nav and `headline.json`.

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
Directory symlinks below either root are refused even when their destination is
inside that root: an alias could otherwise bypass collision checks and replace
the lock inode or another page's output. A symlink naming the root itself is
allowed when the resolved roots remain disjoint. The entire served tree must be
symlink-free, including unrelated and dangling aliases: a same-user HTTP server
could otherwise follow an alias to private files independently of output paths.
An unreadable subtree fails validation. These checks assume trusted filesystem
writers; they do not prevent another process adding aliases after validation or
exposure through hard links, bind mounts, or server-side alias configuration.
The CLI validates all envelope paths, including unselected pages and the separate
portal, before collection.

`load_envelope` reads a stored envelope back and drops `detail`. A missing,
unparsable, wrongly-shaped or unrecognised-status file is a *blind* page (`None`),
which `derive_core` counts in `pages_unknown`; one corrupt page never stops the
others rebuilding.

## `gptme-vitals build [page...]`

```txt
gptme-vitals build [page...] --registry pkg.mod:REGISTRY --emitters pkg.mod:EMITTERS \
    --private-root DIR --public-root DIR --agent NAME [--base-url URL] [--title TEXT]
```

`REGISTRY` is a `RouteRegistry`; `EMITTERS` maps `slug -> () -> PageEnvelope`.
`--base-url` is the absolute HTTP(S) URL the public root is served at. It is
required when the headline route's `href` is relative, because `headline.json`
is pulled by other agents' fleet views and its link must resolve off-host; both
`build` and `timers` refuse up front rather than build pages (or write units)
that could never produce `headline.json`.
Naming pages rebuilds only those. `core.json` and `headline.json` are always
re-derived over **all** pages, the rest read back from the private root, so a
partial build never leaves the fleet view describing only what was just touched.
Reading from the private root (not the served one) means a private page feeding a
dimension counts the same whether or not it was rebuilt this run.

Builds are serialised: collection, publishing, deriving and the shared writes
(`core.json`, `headline.json`, portal) run under an exclusive `flock` on
`<private-root>/.gptme-vitals.lock` (Unix only), because per-page timers share the
top of the hour and an older build finishing second would otherwise replace a newer
view. A slow collector delays other builds; this deliberately trades throughput
for ordered observations without relying on emitter-supplied timestamps. The lock
pathname is reserved against envelope outputs. `build` also refuses to start when
two outputs would write one file (shared `summary_source`,
`core.json`/`headline.json`, the portal page), when an output file is another
output's parent directory, or when no route sets `is_headline`.

Every build removes old public summaries and registered HTML outputs for currently
private routes before collection, even when those pages were not selected or
their emitters fail. HTML cleanup also covers routes without envelopes. Standalone
`publish_page` revokes both registered files for its private route as well.
Revocation also scans the served tree for valid page-envelope objects whose schema
and slug identify the private route. This removes historical summaries after
`summary_source` is renamed or removed, regardless of filename. Unreadable files
stop cleanup; malformed/non-envelope files are not attributed to a route. Historical
HTML paths must still be retained in the registry during revocation or removed by
the operator: HTML does not carry an envelope ownership contract.
When any envelope route is private, the old portal and `headline.json` are also
removed before collection: both can embed text from a formerly public page,
even after headline ownership moves to another route. Leaving them until core
generation succeeds could retain now-private text on a failing build. A successful
build replaces them with the current public-only view; a failed build may leave
them absent.
A cleanup error stops the build with exit 1 rather than claiming successful privacy
revocation. For public pages, a failed served-file write restores the previous
private file byte-for-byte (including invalid UTF-8), or removes a first-time
private write, so derivation does not advance
the failed page. This is error recovery, not a crash-atomic two-root transaction:
readers can observe intermediate generations, and a process crash or a failed
rollback can require operator recovery.

A failing emitter is reported, the other pages still publish, and the failed
page keeps its previous file. Exit status is 1 if any page or the derived files
failed, 2 for a usage error (unknown page, bad `module:ATTR`, overlapping roots).
A page that fails stays at its last published status; it does not turn `stale`
by itself, so schedule a freshness check on `generated_at` if that matters.

## Per-page timers

```txt
gptme-vitals timers --registry … --emitters … --private-root … --public-root … \
    --agent NAME --executable /abs/path/to/gptme-vitals --out DIR [--unit-prefix gptme-vitals] \
    [--base-url URL] [--title TEXT]
```

Writes `<prefix>-<slug>.service` and `.timer` for every envelope route, running
`gptme-vitals build <slug> …` on the route's `cadence` (`5min`…`30min` →
`OnCalendar=*:0/N`, `hourly` → `hourly`). A single whole-tree build would
over-refresh cheap pages or under-refresh expensive ones. It only renders text:
it never calls systemctl and never creates the data roots; you place the files and
enable them.

Services default to `TimeoutStartSec=infinity`: a startup deadline would count
unbounded time waiting behind other collectors on the shared build lock. Collectors
must bound their own I/O; a hung collector otherwise stalls every page. Library
callers can pass `timeout_seconds` to impose an explicit whole-build deadline,
including lock wait, when their deployment has a known queue bound.

Regeneration removes obsolete generated service/timer files for the same prefix,
including removed routes and routes that lost their envelope or schedule. Ownership
is recognized by the generated unit header, not just a filename prefix; unrelated
units and other prefixes remain untouched. This reconciles files only: the operator
must stop/disable previously installed timers and reload systemd separately. Output
filesystem failures return exit 1 with a diagnostic; generation is not transactional
and can leave a partial set, so install only after a successful command.

`manual` and `n/a` routes get no unit and are listed as skipped, so a page nobody
refreshes is visible. The CLI refuses a scheduled envelope route without an
emitter before writing any units: its generated `build <slug>` could never run.
Unscheduled stored-only routes may omit an emitter. Rendering refuses a relative `--executable` (systemd needs
an absolute path), a slug or prefix outside `[a-z0-9-]`, and any argument with a
newline or NUL (a newline would end the `ExecStart` directive and let the next
line be read as a new one). Arguments are quoted by systemd's rules (`%` and `$`
doubled), not `shlex`'s.

## The portal

`render_portal(registry, envelopes, title=…)` is the one renderer: every card is
the page's `status` badge, `headline` and up to four `lines`, so no page needs a
bespoke preview function. Only `PUBLIC` routes get a card, and only those three
fields are read, so `detail` and `core` cannot reach the page even if a caller
hands over unfiltered envelopes. Every envelope string is HTML-escaped (headlines
come from collectors that read ticket titles and error messages). A route with a
`summary_source` but no envelope shows `no data`, never a healthy-looking card.

`gptme-vitals build` writes the portal to the registry's `portal.output_path`
under the public root after `core.json`, from the same envelopes (so a partial
build still shows every page). Pass `--title` to name it. `head_html` and
`toolbar_html` take the theme fragments (`theme.render_head_assets()` /
`render_theme_toggle()`); the CLI passes both, so a built portal carries the
shared tokens and the theme toggle.

## Next in slice 1

Switch Bob's own repo onto the package.
