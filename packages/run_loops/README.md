# run-loops

Python-based run loop framework for autonomous AI agent operation.

## Overview

This package provides infrastructure for running autonomous AI agents with:

- **Autonomous Run Loops**: Base framework for scheduled/triggered agent execution
- **Project Monitoring**: GitHub PR/issue monitoring with automated responses
- **Email Integration**: Email-based communication loops
- **Utilities**: Locking, logging, GitHub API, git operations

## Installation

```bash
uv pip install -e packages/run_loops
```

## Usage

### CLI

```bash
# Run autonomous loop
run-loops autonomous --workspace /path/to/workspace

# Run project monitoring
run-loops project-monitoring --workspace /path/to/workspace

# Run email monitoring  
run-loops email --workspace /path/to/workspace
```

### Python API

```python
from run_loops.autonomous import AutonomousRunner
from run_loops.project_monitoring import ProjectMonitor
from run_loops.email import EmailRunner

# Create and run autonomous loop
runner = AutonomousRunner(workspace="/path/to/workspace")
runner.run()

# Monitor GitHub projects
monitor = ProjectMonitor(workspace="/path/to/workspace")
monitor.run()
```

## Components

### Autonomous Runner (`autonomous.py`)
Main autonomous operation loop that:
- Executes scheduled runs via systemd timers
- Handles task selection and execution
- Manages hot-loop coordination

### Project Monitor (`project_monitoring.py`)
GitHub monitoring that:
- Checks PRs for updates, CI failures, review comments
- Classifies work as GREEN (executable) or RED (needs escalation)
- Executes GREEN work automatically

### Email Runner (`email.py`)
Email-based communication that:
- Syncs with Gmail via mbsync
- Processes incoming emails
- Generates and sends responses

### Utilities (`utils/`)
- `lock.py`: Distributed locking for coordination
- `github.py`: GitHub API wrapper
- `git.py`: Git operations
- `logging.py`: Structured logging
- `prompt.py`: Prompt generation
- `execution.py`: gptme execution wrapper

## Configuration

Run loops are typically configured via systemd timers:

```bash
# Example timer for autonomous runs
~/.config/systemd/user/bob-autonomous.timer
```

See `dotfiles/.config/systemd/user/` in agent workspaces for examples.

## Requirements

- Python >= 3.10
- click >= 8.0.0
- pyyaml >= 6.0.0
- gptme (for execution)
- gh CLI (for GitHub operations)

## License

MIT
