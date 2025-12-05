# Tasks Package

Work queue generation and task management utilities for gptme agents.

## Features

- Generate work queues from task files and GitHub issues
- Prioritize tasks based on priority labels and assignments
- Support for multiple task sources (local files, GitHub)
- Configurable workspace structure

## Installation

As part of gptme-contrib workspace:
```bash
uv sync
```

Or standalone:
```bash
pip install -e packages/tasks
```

## Usage

### Generate Work Queue

```bash
# Basic usage (current directory as workspace)
python3 -m tasks.generate_queue

# Specify workspace path
python3 -m tasks.generate_queue --workspace ~/my-agent

# Specify GitHub username for assignee filtering
python3 -m tasks.generate_queue --github-username YourUsername

# Custom directory names
python3 -m tasks.generate_queue \
    --workspace ~/my-agent \
    --journal-dir journal \
    --tasks-dir tasks \
    --state-dir state
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

- `WORKSPACE_PATH` / `--workspace`: Agent workspace path
- `GITHUB_USERNAME` / `--github-username`: GitHub username for filtering
- `--journal-dir`: Journal directory name (default: journal)
- `--tasks-dir`: Tasks directory name (default: tasks)
- `--state-dir`: State directory name (default: state)

## Task Format

Task files should use frontmatter metadata:
```yaml
---
state: active      # new, active, paused, done
priority: high     # low, medium, high, urgent
---
# Task Title

Task description...
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
cd packages/tasks
make test
```

### Type Checking

```bash
cd packages/tasks
make typecheck
```

## Example

```bash
# Generate queue for agent workspace
python3 -m tasks.generate_queue \
    --workspace /path/to/workspace \
    --github-username AgentUsername

# Output: /path/to/workspace/state/queue-generated.md
```

## Integration

This package is designed to work with:
- gptme autonomous runs
- GitHub issue tracking
- Agent workspace structures

For full autonomous agent setup, see gptme-agent-template.
