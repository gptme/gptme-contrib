# gptme-dashboard

Turn a gptme agent workspace into a browsable website: a static HTML dashboard (GitHub
Pages-ready) plus a `data.json` export, and an optional live server with a JSON API for
monitoring sessions, tasks, journals and services.

**Status:** experimental, actively used. Works on any workspace following the
[gptme-agent-template](https://github.com/gptme/gptme-agent-template) layout; the live
services/schedule panels are Linux (`systemd --user`) first.

## Why / when to use it

An agent workspace accumulates lessons, skills, tasks, journals and summaries as plain files.
`gptme-dashboard` renders all of that into one page that humans can browse and link to, without
running anything on the server side. The design principle is **each agent owns its dashboard**:
the tool produces a self-contained static site you can host anywhere, and the gptme web UI can
link to it via `[agent.urls]` in `gptme.toml`.

It plugs into sibling packages when they are installed:

- [gptodo](../gptodo/README.md) — used to load `tasks/` with proper type coercion (falls back to
  plain frontmatter parsing when absent). Any task files with YAML frontmatter work.
- [gptme-sessions](../gptme-sessions/README.md) — powers the sessions snapshot (`--sessions`) and
  the live `/api/sessions*` endpoints.

Design notes and roadmap: [DESIGN.md](./DESIGN.md) and
[gptme-contrib#382](https://github.com/gptme/gptme-contrib/issues/382).

## Install

Not published on PyPI. Install from the repository:

```bash
# CLI only (static generation)
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-dashboard"

# From a clone of gptme-contrib (needed for the live server, since gptme-sessions
# is also a workspace package)
git clone https://github.com/gptme/gptme-contrib && cd gptme-contrib
uv pip install -e packages/gptme-sessions -e "packages/gptme-dashboard[serve]"
```

Extras: `serve` (Flask + gptme-sessions), `sessions` (gptme-sessions), `tasks` (gptodo).
Requires Python 3.10+.

## Quickstart

```bash
# Writes <workspace>/_site/index.html, data.json and detail pages (plus sitemap.xml and
# feed.xml when a base URL is known, e.g. auto-derived from a GitHub remote)
gptme-dashboard generate --workspace .

# Preview it
python3 -m http.server -d _site 8000
```

`gptme-dashboard --workspace .` (no subcommand) is equivalent to `generate`.

## `generate` options

| Option | Description |
|--------|-------------|
| `--workspace PATH` | Workspace root (default `.`) |
| `--output DIR` | Output directory (default `<workspace>/_site`) |
| `--templates DIR` | Custom Jinja2 template directory (must provide `index.html`) |
| `--json` | Print the JSON data dump to stdout. Without `--output`, HTML generation is skipped |
| `--sessions / --no-sessions` | Include a snapshot of recent agent sessions (needs gptme-sessions; default off) |
| `--sessions-days N` | How many days back to scan for sessions (default 30) |
| `--base-url URL` | Base URL for `sitemap.xml` and Atom `feed.xml`. Auto-derived from the GitHub remote (`https://<owner>.github.io/<repo>/`); pass `-` to suppress both |
| `--include-terminal-tasks` | Include done/cancelled tasks in the index listing (their detail pages are always generated) |

```bash
gptme-dashboard generate --workspace . --json | jq '.stats'
```

## What it shows

Scanned from the workspace (and from nested git submodules that have gptme-like structure —
`lessons/`, `skills/`, `packages/`, `plugins/` or a `gptme.toml` — tagged with a **Source** label):

- **Guidance** — lessons (`lessons/`) and skills (`skills/*/SKILL.md`) in one filterable table,
  each with a rendered detail page
- **Plugins** (`plugins/`, enabled status from `gptme.toml`) and **Packages**
  (`packages/*/pyproject.toml`)
- **Tasks** (`tasks/*.md`) with state, priority, tags, age and detail pages
- **Journals** (`journal/`) and **Summaries** (`knowledge/summaries/{daily,weekly,monthly}/`)
- **README** of the workspace, rendered as an About section
- **KPIs** when present: reads `state/sessions/session-records.jsonl`,
  `state/lesson-thompson/loo-results.json` and `state/weekly-goals.yaml`
- **Community plugins** from `docs/community_plugins.json`, if present
- **Sessions** (only with `--sessions`)

## Live server

```bash
gptme-dashboard serve --workspace .                     # http://127.0.0.1:8042
gptme-dashboard serve --workspace . --host 0.0.0.0 --port 9000
```

`serve` regenerates the static site on start, then serves it alongside a JSON API. The page
detects the API and shows live panels; static deployments are unaffected.

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Agent name, workspace name, `[agent.urls]` |
| `GET /api/sessions/stats[?days=N]` | Aggregated session statistics |
| `GET /api/sessions` | Recent sessions; `limit` (≤200), `offset`, `days`, `model`, `harness`, `outcome` |
| `GET /api/activity[?days=N]` | Daily session counts (7–730 days, default 365) |
| `GET /api/journals[?limit=N]` | Recent journal entries (default 30) |
| `GET /api/tasks[?state=X&limit=N]` | Tasks (default limit 100) |
| `GET /api/summaries[?type=daily\|weekly\|monthly&limit=N]` | Summaries |
| `GET /api/search?q=...[&type=...&limit=N]` | Full-text search over lessons, skills, tasks, journals, summaries, packages, plugins |
| `GET /api/services` | `systemd --user` / launchd services whose name contains `gptme` or the agent name |
| `GET /api/services/health` | Uptime, memory, restart count, recent errors per service (Linux) |
| `GET /api/services/logs?service=NAME` | Recent journal lines; `since` (`1h`/`6h`/`24h`/`7d`), `lines` (≤500), `priority` |
| `GET /api/schedule` | `systemd --user` timers for the same services (Linux) |
| `POST /api/services/<name>/restart` | Restart a matching service — loopback only, requires the `X-Restart-Token` header |

The restart token comes from `GPTME_DASHBOARD_RESTART_TOKEN`, else `[dashboard] restart_token` in
`gptme.toml` (both must be ≥32 chars), else a random token logged at startup.

### Org view (multiple agents)

Pass `--org org.toml` to aggregate several agents' live APIs at `/org` and `/api/org`:

```toml
[[agents]]
name = "alice"
api  = "https://alice.example.com:8042"

[[agents]]
name = "bob"
api  = "https://bob.example.com:8042"
```

## Configuration

Named links in `gptme.toml` appear in the dashboard header (any `http`/`https` URL):

```toml
[agent.urls]
dashboard = "https://example.github.io/my-agent/"
repo      = "https://github.com/example/my-agent"
```

The auto-detected GitHub remote is always shown alongside these.

## Custom templates

`--templates DIR` replaces the bundled templates (`src/gptme_dashboard/templates/`). `index.html`
receives every top-level key of the JSON dump — `workspace_name`, `gh_repo_url`, `agent_urls`,
`readme`, `guidance`, `lessons`, `skills`, `plugins`, `packages`, `tasks` (non-terminal unless
`--include-terminal-tasks`), `journals`, `summaries`, `sessions`, `stats`, `lesson_categories`,
`kpi`, `community_plugins`, `core_files`, `submodules`, `sources` — plus `readme_html` and
`feed_url`. Run `generate --json` to see the exact shape for your workspace.

## Deploying to GitHub Pages

```yaml
- name: Build dashboard
  run: gptme-dashboard generate --workspace . --output _site
- name: Upload Pages artifact
  uses: actions/upload-pages-artifact@v3
  with:
    path: _site
```

gptme-contrib deploys its own dashboard this way; see
[`.github/workflows/dashboard.yml`](../../.github/workflows/dashboard.yml) for a complete workflow.

## Development

```bash
uv run pytest packages/gptme-dashboard/tests/ -v
```
