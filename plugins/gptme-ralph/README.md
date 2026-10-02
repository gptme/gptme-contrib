# gptme-ralph

A [gptme](https://gptme.org) plugin implementing the **Ralph Loop** pattern: work through a long implementation plan one step at a time, starting each step with a fresh agent context, so long-running tasks don't degrade as the context window fills up.

**Status:** experimental.

## Why use it

Long agent sessions suffer from context rot: old tool output, failed attempts and stale reasoning crowd out what matters. A Ralph Loop avoids this by keeping progress in files instead of in the conversation:

1. **Spec + plan** — a spec file says what to build; a plan file lists steps as Markdown checkboxes.
2. **One step per iteration** — each iteration launches a fresh `gptme` (or Claude Code) process whose prompt contains only the spec, the current plan, and the current step.
3. **Progress in files** — the inner agent ticks the step's checkbox (`- [ ]` → `- [x]`) in the plan file and commits its work; the loop re-reads the plan to pick the next step.

Good fit: multi-step implementation work with a clear plan, long autonomous runs. Poor fit: quick one-off tasks, interactive debugging, or work that needs continuous context across steps.

For durable, multi-session task tracking (backlogs, priorities, dependencies), see [gptodo](../../packages/gptodo/README.md); Ralph drives a single plan to completion.

## Install

Point gptme at the plugin in `gptme.toml` (project) or `~/.config/gptme/config.toml` (user):

```toml
[plugins]
paths = ["/path/to/gptme-contrib/plugins/gptme-ralph"]
enabled = ["gptme_ralph"]
```

Alternatively, install the package into the same Python environment as gptme; it registers itself through the `gptme.plugins` entry point:

```sh
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-ralph"
```

Requirements: the `gptme` CLI (default backend) or the `claude` CLI on `PATH`, and `tmux` for background loops.

## Quickstart

The plugin adds a `ralph_loop` tool whose functions the agent calls from Python blocks. Ask gptme to set up and run a loop:

```sh
gptme "use create_project to set up a Ralph Loop for: add JWT authentication to the API, then run it"
```

Which the agent turns into:

```python
spec, plan = create_project("Add JWT authentication to the API")  # writes spec.md + plan.md
run_loop(spec, plan)
```

`create_project` writes `spec.md` from a template and asks the backend LLM for a task-specific `plan.md` (falling back to a generic 5-step template if that fails). Review and edit both files before starting a long loop.

### Plan format

Steps are Markdown checkboxes (numbered `1.` items are also recognised). The first unchecked step is the current one:

```markdown
# Implementation Plan

- [x] Set up FastAPI project structure
- [ ] Implement user model and database
- [ ] Add JWT authentication
- [ ] Add tests
```

### Background loops

```python
run_loop("spec.md", "plan.md", background=True, max_iterations=30)
# -> "Background Ralph Loop started in session: ralph_loop_1a2b3c4d"
check_loop("ralph_loop_1a2b3c4d")   # current output of the tmux pane
stop_loop("ralph_loop_1a2b3c4d")    # kill the tmux session
```

## Functions

| Function | Purpose |
|----------|---------|
| `run_loop(spec_file, plan_file, workspace=None, backend="gptme", max_iterations=50, step_timeout=600, background=False)` | Run the loop. Paths are relative to `workspace` (default: current directory). Returns a result summary, or a tmux session ID when `background=True`. |
| `create_project(task_description, workspace=None, use_llm=True, backend="gptme", model=None)` | Write `spec.md` and `plan.md`; returns `(spec_path, plan_path)`. |
| `create_spec(task_description, output_file="spec.md", workspace=None)` | Write a template spec. |
| `create_plan(task_description, output_file="plan.md", num_steps=5, workspace=None)` | Write a generic 5-step template plan to edit by hand. |
| `check_loop(session_id)` | Show recent output of a background loop. |
| `stop_loop(session_id)` | Stop a background loop. |

The functions live in `gptme_ralph.tools.ralph_loop` if you want to call them from your own Python code.

### Backends

- `backend="gptme"` (default) runs `gptme -n "<prompt>"` for each step.
- `backend="claude"` runs `claude -p --dangerously-skip-permissions --tools default` with the prompt on stdin. That skips Claude Code's permission prompts, so only use it in a workspace you're happy to let the agent modify freely.

## References

- [Ralph Wiggum loops (video)](https://youtu.be/I7azCAgoUHc), where the pattern comes from

## License

MIT
