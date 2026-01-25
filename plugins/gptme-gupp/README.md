# gptme-gupp

Work persistence plugin for gptme - track work across session boundaries using the GUPP pattern.

## Overview

GUPP (based on Gas Town's "Gastown Universal Propulsion Principle") implements a simple but powerful pattern:

> **"If there is work on your hook, YOU MUST RUN IT"**

This plugin enables agents to persist work state across session boundaries, crashes, compactions, and restarts.

## Installation

```bash
# From gptme-contrib
pip install -e plugins/gptme-gupp

# Or add to your gptme.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-gupp/src"]
enabled = ["gptme_gupp"]
```

## Usage

The plugin provides these functions for the `ipython` tool:

```python
# Create a hook when starting work
hook_start("task-id", "Context summary", "Next action to take")

# Update progress during work
hook_update("task-id", current_step="Step 2", next_action="What to do next")

# List pending hooks
hooks = hook_list()

# Complete when done
hook_complete("task-id")

# Check status as formatted summary
status = hook_status()

# Abandon with reason
hook_abandon("task-id", "Reason for abandonment")
```

## How It Works

1. **At session start**: Check for pending hooks and resume work
2. **During work**: Create/update hooks to track progress
3. **On completion**: Clean up hooks
4. **On crash/restart**: Hooks persist and surface in next session

## Hook Storage

Hooks are stored as JSON files in `state/hooks/` within the workspace:
state/hooks/
├── task-1.json
├── task-2.json
└── archive/        # Abandoned hooks
