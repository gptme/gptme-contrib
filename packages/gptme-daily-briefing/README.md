# gptme-daily-briefing

Generic daily-briefing bundle schema and collectors for gptme agents.

This is the upstream extraction of Bob's `collect-daily-briefing.py` morning
pipeline — see [the design doc](https://github.com/ErikBjare/bob/blob/master/knowledge/technical-designs/daily-briefing-pipeline.md)
and the [tracking issue](https://github.com/ErikBjare/bob/issues/676).

## What it provides

- **`schema`** — TypedDicts describing the bundle JSON contract that email
  and voice renderers consume (`BriefingBundle`, `Bullets`, `Analytics`,
  `Workstream`, etc.). Any agent can produce or consume this format.
- **`collectors`** — generic, agent-agnostic data gatherers:
  - `collect_blockers(repo, label)` — open issues by label
  - `collect_active_tasks(workspace_root)` — `gptodo` active/todo tasks
  - `collect_waiting_tasks(workspace_root)` — task frontmatter `waiting_for`
  - `collect_recent_highlights(workspace_root)` — recent commit subjects
  - `collect_session_stats(sessions_dir)` — gptme-sessions count + categories
  - `collect_open_prs(repos, username)` — open PRs by user across repos
  - `collect_graphql_rate_limit()` — GitHub GraphQL budget probe

Agent-specific bits (KPI shape, bandit state, agent persona) stay in each
agent's local wrapper.

## Use

```python
from pathlib import Path
from gptme_daily_briefing.collectors import (
    collect_blockers,
    collect_active_tasks,
    collect_recent_highlights,
)

workspace = Path("/home/agent/agent")
bundle = {
    "bullets": {
        "blockers": collect_blockers("OWNER/REPO", "request-for-erik"),
        "active_tasks": collect_active_tasks(workspace),
        "recent_highlights": collect_recent_highlights(workspace),
    },
}
```

The agent's own wrapper composes the bundle and writes it to
`state/daily-briefing/YYYY-MM-DD.json` (or wherever).

## Bob's reference wrapper

[`scripts/monitoring/collect-daily-briefing.py`](https://github.com/ErikBjare/bob/blob/master/scripts/monitoring/collect-daily-briefing.py)
in Bob's workspace is the reference consumer. It composes the generic
collectors here with Bob-local additions (Thompson sampling bandit tops,
`weekly-goals.py` KPI snapshot, `pr-review-guide.py` rich PR list).
