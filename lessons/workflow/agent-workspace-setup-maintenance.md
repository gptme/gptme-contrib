---
match:
  keywords:
    - "setting up new agent workspace"
    - "initialize gptme agent from template"
    - "new agent from gptme-agent-template"
status: archived
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
# Preferred: use gptme-agent CLI (handles clone + fork.sh automatically)
gptme-agent create ~/my-agent-name --name MyAgent
cd ~/my-agent-name

# Alternative: manual clone + fork.sh
git clone https://github.com/gptme/gptme-agent-template my-agent-name
cd my-agent-name
git submodule update --init --recursive
./fork.sh /path/to/my-agent-name MyAgent  # customizes identity files

# After either method:
# 1. Edit identity files: ABOUT.md, gptme.toml (name/model/prompt), .env.example → .env
# 2. Install deps and hooks
./install-deps.sh --install && cd dotfiles && ./install.sh && cd ..
# 3. Verify key symlinks point to gptme-contrib
ls -la dotfiles/install.sh dotfiles/.config/git/hooks scripts/runs/autonomous/autonomous-loop.sh
```

## Outcome
- Agent workspace correctly initialized from template
- Shared infrastructure stays symlinked to gptme-contrib (auto-updates)
- Agent-specific files (ABOUT.md, systemd services, custom workflows) stay custom

## Related
- Full guide (maintenance, symlinks, troubleshooting): `knowledge/lessons/workflow/agent-workspace-setup-maintenance.md`
- gptme-agent-template: https://github.com/gptme/gptme-agent-template
- Agent Setup Guide: https://gptme.org/docs/agents.html
