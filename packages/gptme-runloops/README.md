# gptme-runloops

Python-based run loop framework for autonomous AI agent operation.

## Overview

This package provides infrastructure for running autonomous AI agents with:

- **Autonomous Run Loops**: Base framework for scheduled/triggered agent execution
- **Project Monitoring**: GitHub PR/issue monitoring with automated responses
- **PR Review**: Versioned review schema, golden corpus, and model evaluation tooling
- **Email Integration**: Email-based communication loops
- **Team Coordination**: Multi-agent team management
- **Utilities**: Locking, logging, GitHub API, git operations

## Installation

```bash
uv pip install -e packages/gptme-runloops
```

## Usage

### CLI

The primary entrypoint is `gptme-runloops`:

```bash
# Run autonomous loop
gptme-runloops autonomous --workspace /path/to/workspace

# Run project monitoring
gptme-runloops monitoring --workspace /path/to/workspace

# Run a single run-item
gptme-runloops run-item --workspace /path/to/workspace

# Run email monitoring
gptme-runloops email --workspace /path/to/workspace

# Run team coordination
gptme-runloops team --workspace /path/to/workspace
```

### Python API

```python
from gptme_runloops.autonomous import AutonomousRunner
from gptme_runloops.project_monitoring import ProjectMonitor
from gptme_runloops.email import EmailRunner
```

## Components

### Core Run Loops

**`autonomous.py`** — Main autonomous operation loop
- Executes scheduled runs via systemd timers
- Handles task selection and execution
- Manages hot-loop coordination

**`project_monitoring.py`** — GitHub monitoring
- Checks PRs for CI failures, review comments, and merge eligibility
- Classifies work as actionable or blocked
- Executes eligible work automatically

**`email.py`** — Email-based communication
- Syncs with Gmail via mbsync
- Processes incoming emails and generates responses

**`team.py`** — Multi-agent team coordination

**`run_item.py` / `run_item_config.py`** — Single run-item executor and config

### PM Infrastructure

**`merge_lifecycle.py`** — PR merge lifecycle state machine

**`pm_bandit.py`** — Bandit-based project monitoring dispatch

**`pm_dispatch.py`** — Dispatch logic for PM work items

**`prompt_templates.py`** — Structured prompt templates for agent runs

**`worker_records.py`** — Worker session record tracking

### PR Review (`pr_review/`)

Phase 0 tooling for evaluating PR reviewer models before deployment:

- **`schema.py`** — Versioned `ReviewArtifact` / `Finding` types; forge-neutral (GitHub and Forgejo adapters produce the same schema)
- **`corpus.py`** — Golden corpus of historical PRs with hand-labeled ground-truth findings for model evaluation

Phase 1 (in progress): CLI runner that produces `ReviewArtifact` JSON locally without publishing to GitHub.

### Utilities (`utils/`)

- `lock.py`: Distributed locking for coordination
- `github.py`: GitHub API wrapper
- `git.py`: Git operations
- `logging.py`: Structured logging
- `prompt.py`: Prompt generation
- `execution.py`: gptme execution wrapper

## Configuration

Run loops are configured via systemd timers. See `dotfiles/.config/systemd/user/` in agent workspaces for examples.

## Requirements

- Python >= 3.10
- click >= 8.0.0
- pyyaml >= 6.0.0
- gptme (for execution)
- gh CLI (for GitHub operations)

## License

MIT
