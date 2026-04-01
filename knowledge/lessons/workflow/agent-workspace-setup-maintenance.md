# Agent Workspace Setup and Maintenance (Companion)

Full reference for `lessons/workflow/agent-workspace-setup-maintenance.md`.

Covers workspace creation, symlink structure, regular maintenance, and troubleshooting.
The primary setup lesson only triggers on new-workspace-creation keywords. Routine
maintenance is handled by `lessons/workflow/agent-workspace-maintenance.md`; details
here are for reference when doing deeper maintenance work.

## Initial Setup

### Option A: gptme-agent CLI (recommended)

```bash
gptme-agent create ~/my-agent-name --name MyAgent
cd ~/my-agent-name
```

This handles the clone + fork.sh customization automatically.

### Option B: Manual clone + fork.sh

```bash
git clone https://github.com/gptme/gptme-agent-template my-agent-name
cd my-agent-name
git submodule update --init --recursive
./fork.sh /path/to/my-agent-name MyAgent
```

### Configure Identity

Update these files for your agent:
- `ABOUT.md` — Agent identity and goals
- `gptme.toml` — Agent configuration (name, model, prompt)
- `.env.example` → `.env` — Environment variables

### Install Dependencies

```bash
./install-deps.sh --install  # Install all dependencies
cd dotfiles && ./install.sh && cd ..  # Install git hooks
```

## Core Infrastructure Symlinks

The following should be **symlinks to gptme-contrib**, not custom files:

### Dotfiles
```text
dotfiles/
├── install.sh → ../gptme-contrib/dotfiles/install.sh ✓
└── .config/git/
    ├── hooks/ → ../../gptme-contrib/dotfiles/.config/git/hooks ✓
    └── allowed-repos.conf → ../../gptme-contrib/.../allowed-repos.conf ✓
```

### Scripts
```text
scripts/
├── tasks.py → ../gptme-contrib/scripts/tasks.py ✓ (deprecated, use gptodo)
└── runs/autonomous/
    └── autonomous-loop.sh → ../../../gptme-contrib/.../autonomous-loop.sh ✓
```

`scripts/runs/autonomous/autonomous-run.sh` often varies per agent — use env vars
(`$WORKSPACE`, `$AGENT_NAME`) instead of hardcoded paths.

## Verification Checklist

```bash
# Verify key symlinks point to gptme-contrib
ls -la dotfiles/install.sh
ls -la dotfiles/.config/git/hooks
ls -la scripts/runs/autonomous/autonomous-loop.sh

# Check submodule status
git submodule status
# Should show: abc1234... gptme-contrib (heads/master)

# Check git hooks are active
git config --global --get core.hooksPath
# Should return: /home/user/.config/git/hooks
```

## Regular Maintenance

### Update gptme-contrib Submodule

```bash
git submodule update --remote gptme-contrib
git -C gptme-contrib log --oneline -10
git add gptme-contrib
git diff --cached --quiet || git commit -m "chore: update gptme-contrib submodule"
```

### Check for New Infrastructure

After updating gptme-contrib:

```bash
ls gptme-contrib/packages/   # New packages
ls gptme-contrib/lessons/    # New lessons
diff <(ls scripts/) <(ls gptme-contrib/scripts/)
```

## When to Symlink vs Keep Custom

**Symlink to gptme-contrib:**
- Generic infrastructure (git hooks, loop scripts)
- Shared utilities (task management, monitoring)
- Installation scripts that work for all agents

**Keep Custom:**
- Agent identity files (ABOUT.md, README.md)
- Agent-specific systemd services
- Custom workflow scripts
- Autonomous run wrapper (use env vars, not hardcoded paths)

## Configuration Best Practices

```bash
# ❌ Wrong: hardcoded paths
WORKSPACE="/home/alice/alice"

# ✅ Right: dynamic from git
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel)}"
AGENT_NAME="${AGENT_NAME:-$(grep -E '^name\s*=' "$WORKSPACE/gptme.toml" | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr '[:upper:]' '[:lower:]' || echo "agent")}"
```

## Troubleshooting

### Symlink Points to Wrong Location
```bash
rm dotfiles/install.sh
ln -sf ../gptme-contrib/dotfiles/install.sh dotfiles/install.sh
```

### Git Hooks Not Running
```bash
cd dotfiles && ./install.sh && cd ..
git config --global core.hooksPath  # Should return ~/.config/git/hooks
```

### Submodule Out of Sync
```bash
git submodule update --init --recursive
```

## Comparing with Reference Agent (Bob)

When unsure about structure:

```bash
gh repo clone ErikBjare/bob ~/bob-ref
find ~/bob-ref -type l -ls | grep gptme-contrib
```

## Related

- Primary lesson: `lessons/workflow/agent-workspace-setup-maintenance.md`
- Maintenance lesson: `lessons/workflow/agent-workspace-maintenance.md`
- gptme-agent-template: https://github.com/gptme/gptme-agent-template
- Agent Setup Guide: https://gptme.org/docs/agents.html
- Git workflow: `lessons/workflow/git-workflow.md`
