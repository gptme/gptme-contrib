# gptme-contrib-lib

Shared Python helpers used by other gptme-contrib packages and scripts: an
input-source framework that turns GitHub issues, email, webhooks, and schedules
into task files, plus a token-bucket rate limiter, source metrics, workspace
path helpers, and the AI-review merge-blocking policy.

**Status:** internal helper library (`0.1.0`). No stable API guarantees and no
PyPI release; it exists so contrib packages and scripts can share code. If you
are looking for a task system, use [gptodo](../gptodo/README.md); for email,
see [gptmail](../gptmail/README.md).

## Install

As a uv workspace member it is available automatically after
`uv sync --all-packages` in a gptme-contrib checkout. Standalone:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-contrib-lib"
```

Dependencies: `click`, `pydantic>=2`, `pyyaml` (and `tomli` on Python 3.10).

## Modules

All modules live under `gptme_contrib_lib` (there is no top-level re-export).

| Module | Contents |
|--------|----------|
| `input_sources` | `InputSource` ABC, `InputSourceManager`, `TaskRequest`, `TaskCreationResult`, `ValidationResult`, `InputSourceType` (`github`, `email`, `webhook`, `scheduler`) |
| `input_source_impl` | `GitHubInputSource` (issues with a label, via the `gh` CLI), `EmailInputSource` (Maildir + sender allowlist), `WebhookInputSource` (file queue), `SchedulerInputSource` (one-off/recurring tasks from a YAML schedule). Each writes a Markdown file into `<workspace>/tasks/` |
| `orchestrator` | `InputSourceOrchestrator` plus a Click entry point that polls all enabled sources |
| `config` | Pydantic models (`InputSourcesConfig`, per-source configs, `RateLimitConfig`, `MonitoringConfig`) loadable via `from_yaml` / `from_toml` / `from_dict`; workspace path helpers |
| `rate_limiter` | `RateLimiter` (token bucket) and `MultiSourceRateLimiter` |
| `monitoring` | `SourceMetrics`, `MetricsCollector`, `HealthChecker` |
| `ai_review_policy` | `blocking_shortfall(rows)` — which AI code-review findings block a merge (P2+ never block; P0/P1 block until disposed, superseded, or both replied to and resolved; unreadable severity fails closed) |

## Input-source orchestrator

There is no console script; run the module directly:

```bash
python -m gptme_contrib_lib.orchestrator --config input-sources.yaml --once
```

- `--config PATH` — YAML file matching `InputSourcesConfig` (top-level keys
  `github`, `email`, `webhook`, `scheduler`, `monitoring`). Without it, all
  sources are enabled with defaults.
- `--once` — poll each source once, print how many tasks were created, and
  exit; otherwise poll continuously.

Example config:

```yaml
github:
  repo: owner/agent
  label: task-request
  poll_interval_seconds: 300
email:
  enabled: false
webhook:
  enabled: false
scheduler:
  schedule_file: ~/.config/gptme-agent/schedule.yaml
```

## Environment variables

Used by the path helpers in `config` as defaults:

| Variable | Default | Used for |
|----------|---------|----------|
| `GPTME_WORKSPACE` | `~/workspace` (`get_workspace_path`, used by the input sources); `get_agent_workspace()` falls back to git-root detection instead | Where task files are written (`<workspace>/tasks/`) |
| `GPTME_CONFIG_DIR` | `~/.config/gptme-agent` | Scheduler `schedule.yaml` |
| `GPTME_DATA_DIR` | `~/.local/share/gptme-agent` | Scheduler state, metrics |
| `GPTME_AGENT_REPO` | `owner/agent` | GitHub source repo |
| `MAILDIR_PATH` | `~/.local/share/mail/agent` | Email source Maildir |

`get_agent_workspace()` handles running from inside a git submodule (such as
gptme-contrib vendored into an agent workspace) by returning the parent repo.

## Backward compatibility

`src/lib` is a symlink to `src/gptme_contrib_lib`, so legacy `from lib.config
import ...` imports keep working when the `src/` directory is on the import path
(source checkouts). The built wheel only contains `gptme_contrib_lib`.
