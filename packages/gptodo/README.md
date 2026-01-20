# gptodo

Task management and work queue generation utilities for gptme agents.

## Features

- Manage tasks with YAML frontmatter metadata
- Generate work queues from task files and GitHub issues
- Prioritize tasks based on priority labels and assignments
- Support for multiple task sources (local files, GitHub)
- Configurable workspace structure
- Rich CLI interface with status, sync, and planning tools

## Installation

### Standalone (recommended)

```bash
# Using uv tool
uv tool install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo

# Using pipx
pipx install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo
```

### As part of gptme-contrib workspace

```bash
uv sync
# Or install editable
pip install -e packages/gptodo
```

## Usage

### Basic Commands

```bash
# Show task status
gptodo status              # All tasks with progress
gptodo status --compact    # Only active/backlog

# List tasks
gptodo list                # List all tasks
gptodo list --sort state   # Sort by state

# Show task details
gptodo show my-task        # Show specific task
gptodo show 5              # Show task by number

# Edit task metadata
gptodo edit my-task --set state active
gptodo edit my-task --add tag feature
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

### Output

Generates `state/queue-generated.md` with:
- **Current Run**: Summary from latest journal entry
- **Planned Next**: Top 5 prioritized tasks
- **Last Updated**: Timestamp

## Task Format

Task files should use frontmatter metadata:
```yaml
---
state: active      # new, active, paused, done, cancelled
priority: high     # low, medium, high
tags: [feature, ai]
depends: [other-task]
---
# Task Title

Task description...

## Subtasks

- [ ] First subtask
- [x] Completed subtask
```

## Task Sources

1. **Local Task Files** (`tasks/*.md`):
   - Filter: priority=high AND state=new/active
   - Uses frontmatter metadata

2. **GitHub Issues**:
   - Filter: label=priority:high AND state=open
   - Boosts score if assigned to configured username

## Configuration

Via command-line arguments or environment variables:

- `WORKSPACE_PATH` / `--workspace`: Agent workspace path
- `GITHUB_USERNAME` / `--github-username`: GitHub username for filtering
- `TASKS_DIR` / `--tasks-dir`: Tasks directory name (default: tasks)
- `STATE_DIR` / `--state-dir`: State directory name (default: state)

## GitHub Integration

Requires GitHub CLI (`gh`) installed and authenticated:
```bash
gh auth login
```

Priority labels:
- `priority:high` - High priority (included in queue)
- `priority:medium` - Medium priority
- `priority:low` - Low priority

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

## Integration

This package is designed to work with:
- gptme autonomous runs
- GitHub issue tracking
- Agent workspace structures (bob, thomas, etc.)

For full autonomous agent setup, see gptme-agent-template.
