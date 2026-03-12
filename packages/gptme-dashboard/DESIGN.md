# gptme-dashboard Design Document

## Overview

`gptme-dashboard` provides a two-layer architecture for agent monitoring:

1. **Per-agent static site + live API** — each agent owns their dashboard
2. **Org view** — a unified view across multiple agents (see [Org View](#org-view-fleet--multi-agent))

The guiding principle: **each agent owns their dashboard**. The `gptme-dashboard` tool generates
a self-contained static site deployable to GitHub Pages. gptme-webui loads the dashboard URL from
`[agent.urls]` in `gptme.toml` to embed it.

---

## Current Architecture (Phases 1–5)

### Static Generation (`generate` command)

```
gptme-dashboard generate --workspace /path/to/agent
```

Scans the workspace and produces `_site/`:
- `index.html` — browseable HTML dashboard
- `data.json` — structured data dump for custom frontends
- Per-item detail pages: lessons, skills, tasks, journals, packages, plugins, summaries

**Workspace scanning**:
- `lessons/` → lesson items with frontmatter metadata
- `skills/` → skill items with SKILL.md frontmatter
- `tasks/` → task items with YAML frontmatter
- `plugins/` → detected + enabled status from `gptme.toml`
- `packages/*/pyproject.toml` → package metadata
- `knowledge/summaries/` → journal summaries
- Nested submodules with `gptme.toml` (e.g. gptme-contrib, gptme-superuser) → merged in with source label

**Output modes**:
- `--output <dir>` — write to directory (default: `<workspace>/_site/`)
- `--json` — print structured JSON to stdout only

### Live Server (`serve` command)

```
gptme-dashboard serve --workspace /path/to/agent --port 8042
```

Serves the static site plus real-time API endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Agent name, mode, and workspace path |
| `GET /api/sessions` | Recent agent sessions with outcome/grade |
| `GET /api/sessions/stats` | Aggregated session stats by model/harness |
| `GET /api/tasks` | Task list with state/priority |
| `GET /api/services` | systemd/launchd service list |
| `GET /api/services/health` | Detailed health metrics per service (memory, restarts, errors) |
| `GET /api/services/restart-enabled` | CSRF token for restart actions |
| `POST /api/services/<name>/restart` | Restart a named service |
| `GET /api/schedule` | Timer/schedule status |
| `GET /api/journals` | Recent journal entries |
| `GET /api/summaries` | Knowledge summaries |

All endpoints return JSON. The frontend polls these and progressively enhances the static HTML.

### Agent Registration

Agents register their dashboard URL and API endpoint in `gptme.toml`:

```toml
[agent.urls]
dashboard = "https://timetobuildbob.github.io/bob/"   # static site (gh-pages)
dashboard-api = "https://bob.example.com:8042"         # live server (optional)
```

gptme-webui reads `dashboard` from `[agent.urls]` and loads it in an iframe/panel.

---

## Org View (Fleet / Multi-Agent)

> **Status**: Phase 6a (standalone `--org` aggregator) is merged. Phase 7b (gptme-webui
> integration) is planned — see [Phase 7b design](#phase-7b-gptme-webui-fleet-integration) below.
>
> **Terminology**: This document uses "org" — it's more general than "team" (scales from 2-person
> teams to large autonomous organizations). "fleet" is taken by gptme.ai k8s infrastructure.
> "team" may be used in UI labels where the shorter word reads better.

### Problem

Agents run on separate VMs/machines. There's no unified view of:
- Which agents are active/idle
- What each is working on
- Service health across the org
- Recent activity (sessions, commits, tasks)

A filesystem-local solution won't work — the design must be distributed and opt-in.

### Architecture

```
                    ┌─────────────────────┐
                    │     gptme-webui     │
                    │     (Org tab)       │
                    └──────────┬──────────┘
                               │ reads org.toml
                               │ calls /api/* on each agent
                    ┌──────────▼──────────┐
                    │   org aggregator    │
                    │  (webui or standalone)
                    └──────┬──────┬───────┘
                           │      │
              ┌────────────▼──┐ ┌─▼────────────┐
              │  Bob's VM     │ │  Alice's VM   │
              │ gptme-dashboard │ │ gptme-dashboard│
              │ serve :8042   │ │ serve :8042   │
              └───────────────┘ └───────────────┘
```

### Agent Card

Each agent in the org view shows:

| Field | Source |
|-------|--------|
| Name | `gptme.toml [agent] name` |
| Status | `/api/services` (active/idle/unknown) |
| Last activity | `/api/sessions` latest timestamp |
| Active tasks | `/api/tasks?state=active` count |
| Running services | `/api/services` filtered |
| Links | dashboard URL, repo, API endpoint |

### Org Configuration

An `org.toml` (or `~/.config/gptme/org.toml`) lists known agents:

```toml
[[agents]]
name = "bob"
api  = "https://bob.example.com:8042"

[[agents]]
name = "alice"
api  = "https://alice.example.com:8042"
```

Agents opt-in by starting `gptme-dashboard serve` and publishing their endpoint.

### gptme-server Integration

**Key question**: should `gptme-dashboard serve` be an extension of `gptme-server` rather than a
separate process?

**Arguments for integration**:
- One server per agent instead of two (dashboard + gptme-server)
- Reuse gptme-server's auth infrastructure (no duplicate auth work)
- gptme already exposes `/api/conversations` — dashboard data fits naturally alongside it
- Simpler deployment: agents already run gptme-server; adding dashboard means just enabling an extension

**Arguments against** (or deferring):
- gptme-server is in gptme core; dashboard is in gptme-contrib — different release cycles
- Dashboard server has OS-specific features (systemd journals) that are better isolated
- Extension API for gptme-server doesn't exist yet

**Current stance**: implement `gptme-dashboard serve` as a standalone server initially. Design the
API so it could be served behind gptme-server as a reverse-proxied extension later without breaking
the API contract. Track as a Phase 6 option.

### Implementation Options

**Option A: gptme-webui "Org" tab** *(primary path)*

Add an "Org" tab to gptme-webui that:
- Reads `org.toml` for agent endpoints
- Calls each agent's `/api/*` directly
- Renders agent cards with status/tasks/services

Advantage: reuses gptme-webui's existing multi-host support and auth infrastructure.

**Option B: `gptme-dashboard serve --org`** *(standalone, no webui dependency)*

```bash
gptme-dashboard serve --org org.toml --port 8090
```

Renders an `/org` page by aggregating each agent's API. Useful for self-hosted setups and as a
stepping stone before Option A.

### Authentication

Remote `gptme-dashboard serve` instances need authentication. Proposed:
- **Phase 6a**: No auth — trust network boundary (internal VMs, VPN)
- **Phase 6b**: Per-agent bearer tokens in `org.toml`
- **Phase 6c**: mTLS for production inter-VM communication; or reuse gptme-server auth if integrated

### Open Questions

1. **Org config location**: per-user (`~/.config/gptme/org.toml`) or per-workspace?
2. **Polling vs SSE**: poll every 30s (simple) or SSE subscriptions (live but complex)?
3. **Implementation order**: Option A (webui) is the primary long-term path; Option B (standalone)
   is already shipped as Phase 6a and serves as a stepping stone.

---

## Phase 7b: gptme-webui Fleet Integration

> Based on discussion in gptme/gptme-contrib#382 (2026-03-11).

### Problem Statement

Agents run on separate VMs/machines. gptme-webui is the natural aggregator since it already has
multi-host support in progress (ErikBjare/bob#252). The standalone `--org` aggregator (Phase 6a)
is useful for headless deployments, but the primary user-facing fleet view should live in
gptme-webui where the rest of the agent interaction happens.

### Design: Discovery via /api/config

Rather than a separate org config file, gptme-webui uses the servers it already knows about:

1. For each connected server, gptme-webui calls `GET /api/config` (from gptme-server)
2. The response includes `[agent.urls]` from `gptme.toml`:
   ```json
   { "agent": { "urls": { "dashboard": "...", "dashboard-api": "http://localhost:8042" } } }
   ```
3. If `dashboard-api` is present, gptme-server proxies `/api/dashboard-proxy/*` → `http://localhost:8042/api/*`
4. gptme-webui fetches agent data via the gptme-server proxy (same-origin, no CORS/mixed-content)

This is **discovery-based** — no separate org.toml or manual URL configuration. The connection
between multi-host gptme-server and per-agent gptme-dashboard is automatic.

**Why proxy through gptme-server instead of direct browser→dashboard-api calls:**
- The browser would need to make cross-origin requests to `http://host:8042` (CORS headers required)
- If gptme-webui is served over HTTPS, direct `http://` dashboard-api calls are blocked by
  mixed-content browser policy
- Proxying through gptme-server avoids both issues: the browser only ever talks to gptme-server
  (already trusted, same-origin relative to gptme-webui), and gptme-server connects to
  `http://localhost:8042` on the same host — a local call that has no TLS or CORS constraint

```
gptme-webui (user's browser)
    │  (all requests are same-origin — no CORS, no mixed-content)
    ├── Server: bob-vm:8140  (gptme-server)
    │   GET /api/config → { agent.urls.dashboard-api: "http://localhost:8042" }
    │   GET /api/dashboard-proxy/status  ──proxy──▶  localhost:8042/api/status
    │   GET /api/dashboard-proxy/sessions ─proxy──▶  localhost:8042/api/sessions
    │
    └── Server: alice-vm:8140  (gptme-server)
        GET /api/config → { agent.urls.dashboard-api: "http://localhost:8042" }
        GET /api/dashboard-proxy/status  ──proxy──▶  localhost:8042/api/status
        GET /api/dashboard-proxy/sessions ─proxy──▶  localhost:8042/api/sessions
```

### Agent Card (Fleet View)

Each agent renders as a card in gptme-webui's "Org" tab:

```
┌──────────────────────────────────────────────┐
│  🟢 Bob                          [Open] [↗]  │
│  Last active: 2 hours ago                     │
│  Active tasks: 3  •  Services: 4/4 healthy    │
│  Working on: gptme-contrib#382 dashboard      │
│  Recent session: productive (2h) — PR opened  │
└──────────────────────────────────────────────┘
```

Fields:
| Field | API source |
|-------|-----------|
| Status (active/idle) | `/api/services` — any service active? |
| Last active | `/api/sessions` — most recent session timestamp |
| Active task count | `/api/tasks?state=active` |
| Service health | `/api/services/health` |
| Current task title | `/api/tasks?state=active` first result |
| Latest session summary | `/api/sessions` first result `.outcome` |

### Agent Command Center Vision

> Inspired by: "I feel a need to have a proper 'agent command center' IDE for teams of them, which
> I could maximize per monitor. I want to see/hide toggle them, see if any are idle, pop open
> related tools (e.g. terminal), stats (usage), etc." — ErikBjare

The fleet view as a command center:

- **Grid layout**: agent cards in a responsive grid, maximize per monitor
- **Status at a glance**: color-coded status (green=active, yellow=idle, red=error)
- **Drill-down**: click card → full dashboard (iframe or new tab)
- **Quick actions**: restart service, reassign task, view logs — without leaving the command center
- **Idle detection**: surface agents with no active tasks ("available for work")
- **Usage/cost metrics**: model calls, tokens, time — per agent per day

This is achievable with the existing `/api/*` endpoints. The main new piece is the gptme-webui
"Org" tab implementation.

### Implementation Plan

**Step 1** (gptme-contrib): No changes needed. Existing API is sufficient.

**Step 2** (gptme/gptme): Extend gptme-server to support the dashboard-proxy integration.
- Expose `agent.urls` from `gptme.toml` in the `/api/config` response (enables webui discovery)
- Add `/api/dashboard-proxy/*` → `http://localhost:8042/api/*` reverse-proxy route (only active when `agent.urls.dashboard-api` is set in `gptme.toml`)
- Together these two changes let gptme-webui discover the dashboard-api URL and fetch agent data without CORS or mixed-content issues

**Step 3** (gptme/gptme): Add "Org" tab to gptme-webui
- For each configured server, probe `GET /api/config` for `agent.urls.dashboard-api`
- If present, fetch agent data via gptme-server's `/api/dashboard-proxy/*` route
- Card renders: status, last activity, active tasks, service health
- "Open dashboard" button links to `agent.urls.dashboard`

**Step 4** (optional, later): Auth for dashboard-proxy route
- gptme-server's existing auth mechanism covers the proxy route — no new config file needed
- If gptme-server requires a bearer token, that same token authenticates dashboard-proxy calls

### What We're NOT Doing

- Not running gptme-dashboard on every agent that doesn't want fleet visibility
- Not requiring a central coordinator/server — each gptme-server proxies only its own agent's API
- Not making the browser talk directly to dashboard-api (avoids CORS and mixed-content issues)
- Not replacing gptme-webui's existing session/conversation view — the "Org" tab is additive
- Not introducing a separate org.toml for discovery — gptme-webui's existing server list is enough

---

## Roadmap

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Static site generator (lessons, skills, packages, plugins) | ✅ merged |
| 2 | Session filtering and pagination | ✅ merged |
| 3 | Schedule/timer monitoring | ✅ merged |
| 4 | Service health monitoring | ✅ merged |
| 5a | Service log viewer | ✅ merged |
| 5b | Service restart actions with auth | ✅ merged |
| 6a | Org view: standalone `--org` aggregator | ✅ merged |
| 6b | Full-text search across workspace content | ✅ merged (#465) |
| 6c | Activity heatmap (daily session counts) | ✅ merged (#466) |
| 6d | gptme-webui Agent Links sidebar | ✅ merged (gptme/gptme#1657) |
| 7a | UX: filter controls hidden until guidance section expanded | 🔄 PR #467 |
| 7b | gptme-webui "Org" tab (fleet-wide view via discovery) | 📋 planned |

---

## Custom Frontends

A core goal: **make it easy to build custom dashboards from scratch**.

`data.json` is the stable data contract:

```json
{
  "workspace": { "name": "bob", "root": "/home/bob/bob" },
  "guidance": [
    { "kind": "lesson", "title": "...", "category": "...", "status": "active", "keywords": [...] }
  ],
  "tasks": [ { "id": "...", "title": "...", "state": "active", "priority": "high" } ],
  "packages": [ { "name": "...", "version": "...", "description": "..." } ],
  "plugins": [ { "name": "...", "enabled": true } ],
  "sessions": [ { "id": "...", "timestamp": "...", "outcome": "productive" } ]
}
```

Any frontend — React, Vue, plain JS — can consume `data.json` without running the Python generator.

Custom dashboards are viewable from within gptme-webui by registering the URL in `[agent.urls]`.
