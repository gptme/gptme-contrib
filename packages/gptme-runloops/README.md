# gptme-runloops

A run-loop framework for operating AI agents autonomously on a schedule: lock-protected
autonomous sessions, GitHub PR/issue monitoring, email handling and team coordination, executed
on interchangeable backends (gptme, Claude Code, Codex, Grok Build), plus a one-shot,
resumable `run` command for any backend.

**Status:** experimental, in production use by the gptme agent fleet. The generic pieces
(`run`, `select`, the backend executors, `BaseRunLoop`, `review`) are reusable as-is; the
`autonomous`, `monitoring`, `run-item` and `team` loops assume a
[gptme-agent-template](https://github.com/gptme/gptme-agent-template)-style workspace and are
more opinionated.

## Why / when to use it

An agent that runs unattended needs more than a cron line: it needs a lock so runs don't overlap,
backoff when the same work keeps failing, a git pull before starting, structured logs, and a way
to swap the underlying agent CLI without rewriting the loop. `gptme-runloops` provides those as a
`BaseRunLoop` class and a CLI you schedule with cron or a systemd timer.

It composes with other contrib packages — use them, or bring your own task and communication
systems:

- [gptodo](../gptodo/README.md) — task management; the `team` loop's coordinator delegates
  through it
- [gptmail](../gptmail/README.md) — the `email` loop syncs and answers mail through it
- [gptme-sessions](../gptme-sessions/README.md) — analyse the sessions these loops produce
- [gptme-coordination](../gptme-coordination/README.md) — work claims between concurrent agents

## Install

Not published on PyPI. Install from the repository:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-runloops"
# or, from a clone of gptme-contrib:
uv pip install -e packages/gptme-runloops
```

The backend CLIs (`gptme`, `claude`, `codex`, `grok`) and `gh` are external and must be on
`PATH` for the backends/loops that use them. The console script is `gptme-runloops`
(`run-loops` is a deprecated alias).

> **Safety:** the loops run their backend non-interactively with permission prompts bypassed
> (e.g. Claude Code with `--dangerously-skip-permissions`). Run them under an account and in a
> workspace where that is acceptable.

## Quickstart: one-shot runs on any backend

```bash
# Run one prompt; prints one JSON line: backend, model, session_id, result,
# exit_code, is_error, resumed, timed_out, cost_usd, agent_output
gptme-runloops run --workspace . --backend claude-code --allowed-tool Read "summarise TODO.md"

# Continue that session (same backend + model)
gptme-runloops run --workspace . --backend claude-code --model <model> --resume <session_id> "and now?"
```

Resume mapping: claude-code `--resume <id>`, codex `codex exec resume <id>`, gptme `--name <id>`.
Backends without resume support (grok-build) fail with a usage error rather than silently
starting fresh. A rejected resume id is an error unless `--on-resume-failure fresh` opts into a
new session (optionally with `--fallback-prompt-file` to carry context over). Tool restriction is
backend-native: `--allowed-tool` (claude-code `--allowedTools`, gptme `--tools`) or `--sandbox`
(codex). Other options: `--prompt-file`, `--timeout` (default 1800 s); `--backend` defaults to
`$AGENT_BACKEND`, else `gptme`. Exit code is 0 on success, 1 on an error result, 2 on usage errors.

## Commands

| Command | Description |
|---------|-------------|
| `run [PROMPT]` | One prompt on any backend, JSON result (see above) |
| `select [--config PATH] [--state-dir DIR] [--json]` | Print the first viable `backend`/`model` from `harness-quota.toml`, skipping candidates whose binary is missing or that are blocked in the block registry. Exit 1 if none |
| `autonomous` | One autonomous session. Prompt from `scripts/runs/autonomous/autonomous-prompt.txt` in the workspace, else a built-in fallback |
| `monitoring [--org ORG]... [--repo OWNER/REPO]... [--author LOGIN] [--agent-name NAME]` | GitHub project monitoring: find PRs/issues needing action (CI failures, review comments, merge eligibility) and act on them. Defaults to the `claude-code` backend |
| `run-item --workspace DIR [--work-file F] ...` | Execute a single project-monitoring work item (`--dry-run` prints the execution plan) |
| `email` | Sync mail (`mbsync -a`), then answer unreplied messages using [gptmail](../gptmail/README.md) |
| `team [--tools LIST]` | Coordinator agent with restricted tools that delegates work to subagents via gptodo |
| `review [--working-tree \| --base SHA --head SHA] [--output F] [--model M]` | LLM review of a local diff, emitted as `ReviewArtifact` JSON (no forge access needed) |
| `review-pr OWNER/REPO#NUM [--shadow\|--publish] [--min-confidence X]` | Review a GitHub PR; `--publish` posts findings as inline comments (default is shadow mode) |

`autonomous`, `email`, `team` and `monitoring` accept `--workspace` (default: current
directory), `--model`, `--tool-format markdown|xml|tool` and `--backend`.

`harness-quota.toml` (default `~/.config/gptme/harness-quota.toml`):

```toml
[[candidates]]
backend  = "claude-code"
model    = "claude-sonnet-4-6"
priority = 1

[[candidates]]
backend  = "gptme"
model    = "openrouter/deepseek/deepseek-chat-v3-0324:free"
priority = 2
```

## Python API

```python
from pathlib import Path
from gptme_runloops import AutonomousRun, get_executor, list_backends

print(list_backends())  # ['claude-code', 'codex', 'gptme', 'grok-build']
run = AutonomousRun(Path("."), executor=get_executor("claude-code"))
exit_code = run.run()
```

Write your own loop by subclassing `BaseRunLoop` and overriding `generate_prompt()` (plus
optionally `has_work()`, `pre_run()`, `post_run()`). The base class provides the lock (in
`<workspace>/logs/`), git pull with retry, timeout handling, logging, and opt-in
consecutive-failure backoff.

Other modules:

- `gptme_runloops.utils.executor` — backend registry (`get_executor`, `list_backends`):
  `execute()` for loops, `run_once()` for one-shot/resumable runs
- `gptme_runloops.gates` — composable runtime-admission gates ("does this triggered session
  deserve to spend inference?"), as pure functions
- `gptme_runloops.triggers` — composable trigger gates ("should a scheduled slot fire at all?")
- `gptme_runloops.pr_review` — versioned, forge-neutral `ReviewArtifact`/`Finding` schema, reviewer,
  verifier, GitHub adapter and a golden corpus for evaluating reviewer models
- `gptme_runloops.pm_dispatch`, `merge_lifecycle`, `pm_bandit`, `worker_records`,
  `prompt_templates` — project-monitoring internals (lane dispatch, PR merge state machine,
  session records)
- `gptme_runloops.utils` — `lock`, `github`, `git`, `logging`, `prompt`, `state`

The legacy import path `run_loops` is kept as a symlink to `gptme_runloops` in source checkouts.

## Development

```bash
uv run pytest packages/gptme-runloops/tests/
```

## License

MIT
