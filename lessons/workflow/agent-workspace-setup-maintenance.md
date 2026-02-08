---
match:
  keywords:
    - workspace
    - setup
    - maintenance
    - symlinks
    - template
    - infrastructure
    - updates
    - dotfiles
    - gptme-contrib
---

# Agent Workspace Setup and Maintenance

This lesson provides a comprehensive guide for setting up a new agent workspace and keeping it properly maintained and aligned with gptme-agent-template and gptme-contrib best practices.

## Initial Setup

### 1. Fork from gptme-agent-template

When creating a new agent workspace:

```bash
# Clone and update submodules
git clone https://github.com/gptme/gptme-agent-template your-agent-name
cd your-agent-name
git submodule update --init --recursive
```

Or use the fork script:
```bash
./fork.sh /path/to/new/agent agent-name
```

### 2. Initial Configuration

Update these files for your agent:
- `ABOUT.md` - Agent identity and goals
- `gptme.toml` - Agent configuration (name, model, prompt)
- `.env.example` → `.env` - Environment variables

### 3. Install Dependencies

```bash
./install-deps.sh --install  # Install all dependencies
cd dotfiles && ./install.sh   # Install git hooks
```

## Core Infrastructure Symlinks

The following should be **symlinks to gptme-contrib**, not custom files:

### Dotfiles
```bash
dotfiles/
├── install.sh → ../gptme-contrib/dotfiles/install.sh ✓
└── .config/git/
    ├── hooks/ → ../../gptme-contrib/dotfiles/.config/git/hooks ✓
    └── allowed-repos.conf → ../../gptme-contrib/.../.../allowed-repos.conf ✓
```

**Custom:** `dotfiles/README.md` - Document both git hooks and your systemd services

### Scripts
```bash
scripts/
├── tasks.py → ../gptme-contrib/scripts/tasks.py ✓ (deprecated, use gptodo)
└── runs/autonomous/
    └── autonomous-loop.sh → ../../../gptme-contrib/.../autonomous-loop.sh ✓
```

**Custom:** `scripts/runs/autonomous/autonomous-run.sh` - But use env vars/config!

## Verification Checklist

### Check Symlinks are Correct
```bash
# From agent workspace root
find . -type l -ls  # List all symlinks

# Verify key symlinks point to gptme-contrib
ls -la dotfiles/install.sh
ls -la dotfiles/.config/git/hooks
ls -la scripts/tasks.py
ls -la scripts/runs/autonomous/autonomous-loop.sh
```

### Check gptme-contrib Submodule
```bash
# Verify submodule is initialized
ls -la gptme-contrib/.git  # Should be a file pointing to ../.git/modules/gptme-contrib

# Check submodule status
git submodule status

# Should show clean status like:
# abc1234567890abcdef123456789 gptme-contrib (heads/master)
```

### Check Git Hooks are Active
```bash
git config --global --get core.hooksPath
# Should return: /home/user/.config/git/hooks

git config --global --get init.templateDir
# Should return: /home/user/.git-templates
```

## Regular Maintenance

### Update gptme-contrib Submodule

Do this regularly (weekly or when you see relevant updates):

```bash
cd your-agent-workspace

# Update submodule to latest
git submodule update --remote gptme-contrib

# Review changes
cd gptme-contrib
git log --oneline -10

# Commit the submodule update
cd ..
git add gptme-contrib
git commit -m "chore: update gptme-contrib submodule"
```

### Check for New Infrastructure

After updating gptme-contrib, check for new shared infrastructure:

```bash
# Compare your scripts with contrib
ls -la scripts/
ls -la gptme-contrib/scripts/

# Compare your dotfiles with contrib
ls -la dotfiles/.config/
ls -la gptme-contrib/dotfiles/.config/

# Check for new packages
ls gptme-contrib/packages/

# Check for new lessons
ls gptme-contrib/lessons/
```

### Audit Your Symlinks

Periodically verify your symlinks are still correct:

```bash
# Run this from your workspace root
cat > /tmp/check-symlinks.sh << 'EOF'
#!/bin/bash
WORKSPACE=$(pwd)

echo "=== Checking Required Symlinks ==="

check_symlink() {
    local path="$1"
    local expected_target="$2"

    if [ -L "$path" ]; then
        actual_target=$(readlink "$path")
        if [ "$actual_target" = "$expected_target" ]; then
            echo "✅ $path → $actual_target"
        else
            echo "❌ $path → $actual_target (expected: $expected_target)"
        fi
    else
        echo "❌ $path is not a symlink (should be → $expected_target)"
    fi
}

check_symlink "dotfiles/install.sh" "../gptme-contrib/dotfiles/install.sh"
check_symlink "dotfiles/.config/git/hooks" "../../gptme-contrib/dotfiles/.config/git/hooks"
check_symlink "scripts/runs/autonomous/autonomous-loop.sh" "../../../gptme-contrib/scripts/runs/autonomous/autonomous-loop.sh"

echo ""
echo "=== Deprecated Symlinks (consider removing) ==="
if [ -L "scripts/tasks.py" ]; then
    echo "⚠️  scripts/tasks.py is symlinked (deprecated - use gptodo instead)"
fi
EOF

bash /tmp/check-symlinks.sh
rm /tmp/check-symlinks.sh
```

## Configuration vs Hardcoding

### ❌ Wrong: Hardcoded Paths
```bash
# BAD - hardcoded in autonomous-run.sh
WORKSPACE="/home/alice/alice"
LOCKFILE="/tmp/alice-autonomous.lock"
```

### ✅ Right: Environment Variables & Config
```bash
# GOOD - configurable via env vars or gptme.toml
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
AGENT_NAME="${AGENT_NAME:-$(grep -E '^name\s*=' "$WORKSPACE/gptme.toml" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr '[:upper:]' '[:lower:]' || echo "agent")}"
LOCKFILE="${LOCKFILE:-/tmp/${AGENT_NAME}-autonomous.lock}"
```

Benefits:
- Portable across different agents
- Works when workspace moves
- Can override for testing
- Easier to maintain

## When to Symlink vs Keep Custom

### Symlink to gptme-contrib:
- ✅ Generic infrastructure (git hooks, loop scripts)
- ✅ Shared utilities (task management, monitoring)
- ✅ Installation scripts that work for all agents
- ✅ Pre-commit validation scripts (if not customized)

### Keep Custom:
- ✅ Agent identity files (ABOUT.md, README.md)
- ✅ Agent-specific systemd services
- ✅ Custom workflow scripts
- ✅ Dotfiles README.md (documents both systems)
- ✅ Autonomous run wrapper (but use env vars!)

### Gray Area - Check with Team:
- ⚠️ Pre-commit scripts (if heavily customized)
- ⚠️ Context generation scripts (if agent-specific logic)
- ⚠️ Monitoring scripts (if custom integrations)

## Troubleshooting

### Symlink Points to Wrong Location
```bash
# Remove broken symlink
rm dotfiles/install.sh

# Recreate correct symlink
ln -sf ../gptme-contrib/dotfiles/install.sh dotfiles/install.sh
```

### Git Hooks Not Running
```bash
# Reinstall hooks
cd dotfiles && ./install.sh

# Verify global config
git config --global core.hooksPath
git config --global init.templateDir
```

### Submodule Out of Sync
```bash
# Reset submodule to committed version
git submodule update --init --recursive

# Or update to latest
git submodule update --remote gptme-contrib
```

### Script Has Hardcoded Paths
```bash
# Check for hardcoded paths
grep -r "/home/$(whoami)" scripts/

# Update to use environment variables or config file
```

## Example: Comparing with Reference Agent (Bob)

When unsure about structure, compare with Bob's workspace:

```bash
# Clone Bob's workspace for reference
cd ~ && gh repo clone ErikBjare/bob

# Compare structures
diff -r ~/your-agent/dotfiles/ ~/bob/dotfiles/
diff -r ~/your-agent/scripts/ ~/bob/scripts/

# Check Bob's symlinks
find ~/bob -type l -ls | grep gptme-contrib
```

## Automated Maintenance

Consider creating a maintenance script:

```bash
# scripts/maintenance/update-infrastructure.sh
#!/bin/bash
set -e

echo "🔄 Updating gptme-contrib submodule..."
git submodule update --remote gptme-contrib

echo "🔍 Checking for new infrastructure..."
# Compare your structure with latest contrib
# Alert if new shared scripts are available

echo "✅ Verifying symlinks..."
# Run symlink verification script

echo "📦 Checking for new packages..."
ls gptme-contrib/packages/

echo "✅ Infrastructure maintenance complete!"
```

## Related Documentation

- gptme-agent-template: https://github.com/gptme/gptme-agent-template
- gptme-contrib: https://github.com/gptme/gptme-contrib
- Agent Setup Guide: https://gptme.org/docs/agents.html
- Lesson: `lessons/workflow/git-workflow.md` - Git best practices
- Lesson: `lessons/workflow/git-worktree-workflow.md` - Worktree management

## Summary

**Core Principle:** Symlink generic infrastructure, keep agent-specific customizations, configure via environment variables/config files instead of hardcoding.

**Regular Tasks:**
1. Update gptme-contrib submodule weekly
2. Verify symlinks are correct
3. Check for new shared infrastructure
4. Audit scripts for hardcoded paths
5. Compare with template/Bob for best practices

By following these practices, your agent workspace stays maintainable, portable, and aligned with the latest gptme infrastructure improvements.
