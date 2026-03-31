---
match:
  keywords:
    - "setting up new agent workspace"
    - "fork from gptme-agent-template"
    - "create new gptme agent"
    - "agent workspace initial setup"
status: active
---

# Agent Workspace Setup

## Rule
When creating a new agent workspace from gptme-agent-template, follow the structured setup process: clone template, configure identity files, install deps, verify symlinks.

## Context
When starting a brand new agent workspace — forking from gptme-agent-template to create a fresh agent. Does NOT apply to routine submodule updates or dotfiles maintenance on existing workspaces.

## Detection
- Creating a new agent repo or forking gptme-agent-template
- First-time workspace initialization
- Setting up agent identity files (ABOUT.md, gptme.toml) for the first time

## Pattern
```bash
# 1. Clone template
git clone https://github.com/gptme/gptme-agent-template your-agent-name
cd your-agent-name
git submodule update --init --recursive

# 2. Configure identity
# Edit: ABOUT.md, gptme.toml (name/model/prompt), .env.example → .env

# 3. Install deps and hooks
./install-deps.sh --install
cd dotfiles && ./install.sh

# 4. Verify key symlinks point to gptme-contrib
ls -la dotfiles/install.sh dotfiles/.config/git/hooks scripts/runs/autonomous/autonomous-loop.sh
```

## Outcome
- Agent workspace correctly initialized from template
- Shared infrastructure stays symlinked to gptme-contrib (auto-updates)
- Agent-specific files (ABOUT.md, systemd services, custom workflows) stay custom

## Related
- gptme-agent-template: https://github.com/gptme/gptme-agent-template
- Agent Setup Guide: https://gptme.org/docs/agents.html
