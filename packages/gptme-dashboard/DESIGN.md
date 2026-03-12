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

> **Status**: Design phase. Implementation in Phase 6.
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
3. **Implementation order**: Option A (webui) requires coordinating a webui PR; Option B (standalone) is self-contained and faster to ship

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
| 7a | UX: filter controls hidden until guidance section expanded | ✅ merged |
| 7b | gptme-webui "Org" tab (fleet-wide view) | 📋 planned |

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
