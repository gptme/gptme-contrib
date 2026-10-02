# gptme-bob-status

Bob-specific `StatusProvider` plugin for `gptme-util status` — adds an agent's
active tasks, open-PR queue, service health, blockers and recent journal
entries to the status report.

**Status:** Bob-specific / internal (alpha, `0.1.0`). It hardcodes Bob's tracked
repos and service names, so it is mainly useful to Bob and as a **reference
implementation** for writing a status provider for your own agent.

## How it works

gptme discovers status providers through the `gptme.status_providers`
entry-point group. This package registers one:

```toml
[project.entry-points."gptme.status_providers"]
bob = "gptme_bob_status.provider:make_provider"
```

The provider only contributes data inside an agent workspace — detected by a
`tasks/` directory and a `gptme.toml` at the git root. Everywhere else it
returns nothing, so it is safe to install globally.

Data sources (each is skipped quietly if the tool is missing or fails):

| Section | JSON key | Source |
|---------|----------|--------|
| Active tasks | `bob_active_tasks` | `gptodo status --compact` ([gptodo](../gptodo/README.md)) |
| Blockers | `bob_blockers` | `gptodo ready --state waiting --jsonl` (tasks with `waiting_for`) |
| Ready next | `bob_ready_tasks` | `gptodo ready --state backlog --jsonl` |
| PR queue | `bob_pr_queue` | `gh pr list` for your open PRs in a fixed set of repos, with an attention watermark per repo |
| Services | `bob_services`, `bob_dead_timers` | `systemctl --user` (Bob's service and `bob-*` timer units) |
| Journal (JSON only) | `bob_journal_entries` | Latest files under `journal/YYYY-MM-DD/` |

The PR-queue watermark is a "triage this queue for stale or red PRs" signal,
not a cap on opening PRs.

## Install

Requires `gptme>=0.29`. From a gptme-contrib checkout:

```bash
uv pip install -e packages/gptme-bob-status
# or
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-bob-status"
```

## Usage

No configuration — once installed, the provider is picked up automatically.
Run from inside the agent workspace:

```bash
gptme-util status          # narrative report (all sections except the journal list)
gptme-util status --json   # machine-readable, includes the bob_* keys
```

## Writing your own provider

Copy the shape of `src/gptme_bob_status/provider.py`: a class with a `name`, a
`collect() -> dict` method for JSON output and a `narrative_sections() -> list[str]`
method returning Markdown sections, plus a zero-argument factory registered
under `gptme.status_providers` in your package's `pyproject.toml`. Swap in your
own task system, repos and services — [gptodo](../gptodo/README.md) is one
option for tasks, but any source works.
