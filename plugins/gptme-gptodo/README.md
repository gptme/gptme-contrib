# gptme-gptodo

**A gptme plugin for running a "coordinator" agent that delegates work to sub-agents
through [gptodo](../../packages/gptodo/README.md).** It exposes the gptodo CLI as a few
Python functions, so the top-level agent can hand out tasks and track them instead of
editing code itself.

**Status:** experimental. It is a thin wrapper around the `gptodo` CLI, and some wrapped
calls have drifted from the CLI they call (see [Known issues](#known-issues)). For
everyday task management, use the `gptodo` CLI directly from the agent's shell. This
plugin is only needed for coordinator-only setups.

## What it provides

A gptme tool named `gptodo` that registers these functions in gptme's Python (`ipython`)
tool:

| Function | Runs |
|----------|------|
| `delegate(prompt, task_id=None, backend="gptme", agent_type="execute", timeout=600, background=True)` | `gptodo spawn <task_id> --prompt ...` in the background or `gptodo run ...` in the foreground (broken without `task_id`, see below). `backend` is `gptme` or `claude`. `agent_type` is `general`, `explore`, `plan` or `execute`. |
| `check_agent(session_id)` | `gptodo status <session_id>` (broken, see below) |
| `list_agents()` | `gptodo agents --json` |
| `list_tasks(state="active")` | `gptodo list`, plus `--active-only` when `state="active"` |
| `task_status(compact=True)` | `gptodo status [--compact]` |
| `add_task(title, description="", priority="medium", task_type="action")` | `gptodo add <title> --priority ... --type ...` |

The tool also gives the model instructions for working as a coordinator: break the work
down, delegate focused sub-tasks, monitor them, then synthesize the results.

## Requirements

- gptme
- One of:
  - the `gptodo` CLI on `PATH` (see [gptodo's install section](../../packages/gptodo/README.md#install))
  - this plugin running from a gptme-contrib checkout with `uv` available, in which case
    it falls back to `uv run python3 -m gptodo` inside `packages/gptodo`

  If neither is found, the tool reports itself as unavailable.
- For background delegation, everything `gptodo spawn` needs: `tmux` and the chosen
  backend CLI (`gptme` or `claude`).

## Install

Point gptme at the plugin directory and enable it in `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-gptodo"]
enabled = ["gptme_gptodo"]
```

The package also registers a `gptme.plugins` entry point (`gptme_gptodo`), so installing
it into the same environment as gptme works as well.

## Usage

The functions are called from Python code blocks, so enable the `ipython` tool alongside
this one:

```bash
gptme --tools gptodo,ipython,save "coordinate work on project X"
```

Example of what the coordinator runs, with an existing task ID:

```python
task_status()
delegate("Fix the failing test in tests/test_auth.py by updating the mock",
         task_id="fix-auth-test", agent_type="execute")
list_tasks()
```

Use the `gptodo` CLI to see what spawned sessions are doing: `gptodo sessions`,
`gptodo output <session_id>` and `gptodo kill <session_id>`.

## Known issues

The following wrapped calls no longer match the current `gptodo` CLI, so treat them as
broken until they are fixed:

- **`delegate()` without `task_id`** passes `--inline`, which `gptodo spawn` and
  `gptodo run` do not accept. Create the task first (`add_task(...)` or `gptodo add`) and
  pass its ID.
- **`check_agent(session_id)`** calls `gptodo status <session_id>`, but `status` takes no
  session argument. Use `gptodo output <session_id>` or `gptodo sessions` instead.
- **`add_task(..., description="...")`** passes `--description`, which `gptodo add` does
  not accept. Leave `description` empty, or pipe the body into `gptodo add` from a shell.
- **`list_agents()`** lists agents registered in `state/agents/` (`gptodo agents`), not
  sessions started with `delegate()`. Those appear in `gptodo sessions`.
- **`list_tasks(state=...)`** only filters for `"active"`. Any other value lists all tasks.

## See also

- [gptodo](../../packages/gptodo/README.md): the task CLI this plugin wraps, including the
  task file format, states, and the full command reference.
- [docs/DESIGN-multi-agent-coordination.md](docs/DESIGN-multi-agent-coordination.md): the
  original design notes for multi-agent coordination on top of gptodo (draft, partly
  implemented).
- [gptme-agent-template](https://github.com/gptme/gptme-agent-template): an agent workspace
  that uses gptodo for task management.
