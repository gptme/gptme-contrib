# gptodo

Task management and work queue generation utilities for gptme agents.

## Features

- Manage tasks with YAML frontmatter metadata
- Generate work queues from task files and GitHub issues
- Prioritize tasks based on priority labels and assignments
- Task locking for multi-agent coordination
- Support for multiple task sources (local files, GitHub)
- Configurable workspace structure

## Installation

### Standalone (Recommended)

Install as a CLI tool:

```bash
# Using uv (recommended)
uv tool install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo

# Using pipx
pipx install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo
```

### From gptme-contrib workspace

```bash
# Install with workspace
uv sync

# Or install package directly
uv pip install -e packages/gptodo
```

## Usage

### Task Management CLI

```bash
# View task status
gptodo status
gptodo status --compact

# Show specific task
gptodo show <task-id>

# Edit task metadata
gptodo edit <task-id> --set state active
gptodo edit <task-id> --set priority high
gptodo edit <task-id> --add tag feature

# Validate tasks
gptodo validate

# List tasks with filters
gptodo list --priority high
gptodo list --state active
```

### Generate Work Queue

```bash
# Basic usage (current directory as workspace)
gptodo generate-queue

# Specify workspace path
gptodo generate-queue --workspace ~/my-agent

# Specify GitHub username for assignee filtering
gptodo generate-queue --github-username YourUsername
```

### Task Locking (Multi-Agent)

```bash
# Acquire lock on a task
gptodo lock acquire <task-id>

# Release lock
gptodo lock release <task-id>

# Check lock status
gptodo lock status <task-id>
```

### Output

Generates `state/queue-generated.md` with:
- **Current Run**: Summary from latest journal entry
- **Planned Next**: Top 5 prioritized tasks
- **Last Updated**: Timestamp

### Task Sources

1. **Local Task Files** (`tasks/*.md`):
   - Filter: priority=high/urgent AND state=new/active
   - Uses frontmatter metadata

2. **GitHub Issues**:
   - Filter: label=priority:high/urgent AND state=open
   - Boosts score if assigned to configured username

## Configuration

Via command-line arguments or environment variables:

- `TASKS_REPO_ROOT` / `--workspace`: Agent workspace path
- `GITHUB_USERNAME` / `--github-username`: GitHub username for filtering
- `--journal-dir`: Journal directory name (default: journal)
- `--tasks-dir`: Tasks directory name (default: tasks)
- `--state-dir`: State directory name (default: state)

## Task Format

Task files should use frontmatter metadata:

```yaml
---
state: active      # new, active, paused, done, cancelled, someday
priority: high     # low, medium, high
task_type: project # project (multi-step) or action (single-step)
assigned_to: bob   # agent name
tags: [ai, dev]    # categorization tags
---
# Task Title

Task description...

## Subtasks
- [ ] First subtask
- [x] Completed subtask
```

## GitHub Integration

Requires GitHub CLI (`gh`) installed and authenticated:

```bash
gh auth login
```

Priority labels:
- `priority:urgent` - Highest priority
- `priority:high` - High priority
- `priority:medium` - Medium priority (not included in queue)
- `priority:low` - Low priority (not included in queue)

## Development

### Running Tests

```bash
cd packages/gptodo
make test
```

### Type Checking

```bash
cd packages/gptodo
make typecheck
```

## Migration from tasks

If you were using `scripts/tasks.py`, the wrapper script will continue to work
but will show a deprecation warning. To migrate:

1. Install gptodo directly: `uv tool install git+...`
2. Replace `./scripts/tasks.py` calls with `gptodo`
3. All commands remain the same

## Integration

This package is designed to work with:
- gptme autonomous runs
- GitHub issue tracking
- Agent workspace structures

For full autonomous agent setup, see [gptme-agent-template](https://github.com/gptme/gptme-agent-template).
