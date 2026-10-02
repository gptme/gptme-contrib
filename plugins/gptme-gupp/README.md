# gptme-gupp — resume in-flight work across sessions, crashes and compactions

Lets an agent leave itself a small "work hook" — what it was doing, the context,
and the exact next action — as a JSON file in the workspace, so the next session
(after a crash, restart, or context compaction) picks up where the last one
stopped.

**Status:** experimental. Small and dependency-free apart from gptme.

The pattern is borrowed from Gas Town's "Gastown Universal Propulsion
Principle": *if there is work on your hook, you must run it.*

> "Hook" here means a work-in-progress marker. It is unrelated to gptme's
> lifecycle hook system (see [gptme-hooks-examples](../gptme-hooks-examples/README.md)).

## Why / when to use it

Long-running or autonomous agents lose their place when a session ends
mid-task. A task tracker records *what* needs doing; a GUPP hook records *where
you are in it right now* and *what to do next*. Use it alongside a task system
such as [gptodo](../../packages/gptodo/README.md) (or your own): one hook per
in-flight task, deleted when the task step completes.

## Install

There is no package entry point; load it as a folder plugin from a
gptme-contrib checkout:

```toml
# gptme.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-gupp"]
enabled = ["gptme_gupp"]   # only needed if you use an allowlist
```

This registers a `gupp` tool whose functions are callable from gptme's Python
(ipython) tool.

## Quickstart

```python
print(hook_status())   # at session start: anything left from last time?

hook_start("fix-auth-bug", "User login failing after token refresh",
           "Debug auth middleware", priority="high")
hook_update("fix-auth-bug", current_step="Root cause found",
            next_action="Add regression test")
hook_complete("fix-auth-bug")                       # deletes the hook
# or: hook_abandon("fix-auth-bug", "Superseded by #123")  # archives it
```

Nothing runs automatically: to make "check hooks first" a habit, tell the agent
to call `hook_status()` at session start (e.g. in its system prompt or context
script).

## Functions

| Function | Purpose |
|----------|---------|
| `hook_start(task_id, context_summary, next_action, current_step="Starting", priority="medium")` | Create a hook (`priority`: `low`/`medium`/`high`) |
| `hook_update(task_id, current_step=None, context_summary=None, next_action=None, partial_results=None)` | Update fields of an existing hook |
| `hook_complete(task_id)` | Delete the hook |
| `hook_abandon(task_id, reason)` | Move the hook to `archive/` with the reason |
| `hook_list()` | Pending hooks as dicts, high priority first, then most recently updated |
| `hook_status(stale_threshold_hours=24)` | Markdown summary; flags hooks not updated within the threshold as stale |

## Storage

Hooks live in `state/hooks/` relative to the current working directory (run
gptme from the workspace root), one `<task_id>.json` per hook:

```text
state/hooks/
├── fix-auth-bug.json
└── archive/          # abandoned hooks, <task_id>-<timestamp>.json
```
