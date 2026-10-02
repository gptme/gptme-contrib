# gptme-daily-briefing

Building blocks for an agent's **daily briefing / morning standup**: a typed
JSON bundle schema plus small collectors for blockers, active and waiting tasks,
recent commits, session stats, and open PRs. Agents compose them in their own
script and render the bundle however they like (email, voice, chat).

**Status:** experimental (`0.1.0`, alpha). A library only — there is no CLI and
no renderer in this package; you write the thin wrapper that assembles and
delivers the bundle.

## What it provides

- **`gptme_daily_briefing.schema`** — `TypedDict`s describing the bundle JSON
  contract (`BriefingBundle`, `Bullets`, `WaitingTask`, `Analytics`,
  `SessionStats`, `Workstream`, `OpenPR`; all re-exported from the package
  root). Producers write it once; email/voice renderers read it without
  re-querying upstream data. `analytics` and `workstream` accept agent-specific
  extra keys.
- **`gptme_daily_briefing.collectors`** — agent-agnostic gatherers. Each one
  fails soft (returns an empty value) instead of raising:

| Collector | Returns | Needs |
|-----------|---------|-------|
| `collect_blockers(repo, label, limit=6)` | `["#570: Title", ...]` — open issues (not PRs) with a label | `gh` CLI |
| `collect_active_tasks(workspace_root, limit=6)` | ids of `active`/`todo` tasks | [gptodo](../gptodo/README.md) runnable via `uv run gptodo` in the workspace |
| `collect_waiting_tasks(workspace_root, limit=8)` | `[{"task", "waiting_for"}]` from `tasks/*.md` frontmatter with `state: waiting` | gptodo-style task files |
| `collect_recent_highlights(workspace_root, limit=6)` | recent commit subjects from `origin/master` → `origin/main` → `master` → `main` | git |
| `collect_session_stats(sessions_dir, days=1)` | `{"count", "categories"}` (or `{"count": 0, "error"}`) | `[sessions]` extra ([gptme-sessions](../gptme-sessions/)) |
| `collect_open_prs(repos, username, limit_per_repo=5)` | `[{"repo", "number", "title", "draft", "url"}]`, paginated | `gh` CLI |
| `collect_graphql_rate_limit()` | GitHub GraphQL rate-limit dict or `None` | `gh` CLI |

The task collectors assume a [gptodo](../gptodo/README.md)-style `tasks/`
directory (Markdown files with YAML frontmatter). If you use a different task
system, skip them and fill `bullets.active_tasks` / `bullets.waiting_tasks`
from your own source — the schema doesn't care where the data comes from.

## Install

Not published to PyPI. Install from the repository subdirectory:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-daily-briefing"
# only needed for collect_session_stats (gptme-sessions is not on PyPI either):
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-sessions"
```

The `[sessions]` extra just declares that `gptme-sessions` dependency; outside
the workspace pip cannot resolve it from PyPI, so install it from git as above.
In a gptme-contrib checkout it is a uv workspace member
(`uv sync --all-packages` pulls in both).

## Usage

```python
import json
from datetime import date, datetime, timezone
from pathlib import Path

from gptme_daily_briefing import BriefingBundle
from gptme_daily_briefing.collectors import (
    collect_active_tasks,
    collect_blockers,
    collect_open_prs,
    collect_recent_highlights,
    collect_waiting_tasks,
)

workspace = Path("~/my-agent").expanduser()
bundle: BriefingBundle = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "date": date.today().isoformat(),
    "bullets": {
        "blockers": collect_blockers("OWNER/REPO", "needs-human"),
        "active_tasks": collect_active_tasks(workspace),
        "waiting_tasks": collect_waiting_tasks(workspace),
        "recent_highlights": collect_recent_highlights(workspace),
    },
    "workstream": {"open_prs": collect_open_prs(["OWNER/REPO"], "my-agent-bot")},
}

out = workspace / "state" / "daily-briefing" / f"{bundle['date']}.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(bundle, indent=2))
```

`state/daily-briefing/<date>.json` is the conventional location; renderers
(an email via [gptmail](../gptmail/README.md), a spoken summary via
[gptme-voice](../gptme-voice/README.md), etc.) read that file.

## Scope

Agent-specific pieces — KPI snapshots, scheduling/bandit state, persona, and
the delivery step — belong in each agent's own wrapper and go under the
open-shape `analytics` / `workstream` keys. Nothing in this package imports
agent-local code.

For narrative summaries of what an agent did (journals, GitHub activity,
sessions), see [gptme-activity-summary](../gptme-activity-summary/).
