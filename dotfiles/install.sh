#!/bin/bash
# Install dotfiles for gptme agent
# Creates symlinks for global git hooks and configuration
#
# Safety: This script includes checks to prevent accidentally running
# on non-agent systems and overwriting user configurations.

set -e

DOTFILES_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# --- Safety Check: Detect if running on agent system ---
# Override with DOTFILES_FORCE=1 to bypass safety check
check_agent_environment() {
    # If forced, skip check
    if [ "$DOTFILES_FORCE" = "1" ]; then
        echo -e "${YELLOW}⚠️  Safety check bypassed (DOTFILES_FORCE=1)${NC}"
        return 0
    fi

    # Check for GPTME_AGENT environment variable (set by agent dotfiles)
    if [ -n "$GPTME_AGENT" ]; then
        return 0
    fi

    # Check reliable agent indicators
    local is_agent=false
    local indicators_found=""

    # Check 1: gptme.toml with agent configuration (most reliable)
    if [ -f "$DOTFILES_DIR/../gptme.toml" ]; then
        if grep -q '\[agent\]' "$DOTFILES_DIR/../gptme.toml" 2>/dev/null; then
            is_agent=true
            indicators_found="${indicators_found}  ✓ gptme.toml with [agent] section\n"
        fi
    fi

    # Check 2: Agent autonomous service running (very specific to agents)
    if systemctl --user is-active '*-autonomous.service' &>/dev/null || \
       systemctl --user list-units --type=service --all 2>/dev/null | grep -q 'autonomous.service'; then
        is_agent=true
        indicators_found="${indicators_found}  ✓ Agent autonomous service detected\n"
    fi

    # Check 3: Running in VM (common for agents, but not definitive)
    if [ -f "/sys/class/dmi/id/chassis_type" ]; then
        chassis_type=$(cat /sys/class/dmi/id/chassis_type 2>/dev/null || echo "")
        # Type 1 = Other/VM
        if [ "$chassis_type" = "1" ]; then
            # VM alone is not sufficient, but contributes to detection
            indicators_found="${indicators_found}  ~ VM environment (chassis_type=1)\n"
        fi
    fi

    if [ "$is_agent" = false ]; then
        echo -e "${YELLOW}⚠️  Safety Warning: Agent environment not detected${NC}"
        echo ""
        echo "This script is designed for gptme agent workspaces."
        echo "It will modify global git configuration and may conflict"
        echo "with existing user configurations."
        echo ""
        echo "Indicators checked:"
        echo "  - gptme.toml with [agent] section: not found"
        echo "  - Agent autonomous service (*-autonomous.service): not found"
        echo "  - GPTME_AGENT environment variable: not set"
        echo ""
        echo "To proceed anyway, set environment variable:"
        echo "  GPTME_AGENT=1 $0"
        echo ""
        echo "Or force installation:"
        echo "  DOTFILES_FORCE=1 $0"
        echo ""
        echo "Or confirm you want to install on this system:"
        read -p "Continue installation? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Installation cancelled."
            exit 1
        fi
    else
        echo -e "${GREEN}✓ Agent environment detected:${NC}"
        echo -e "$indicators_found"
    fi
}

# --- Repo-local core.hooksPath silently replaces the global dir ---
# prek and similar tools set core.hooksPath=.git/hooks, which is why Alice and
# Gordon currently get 0 Git-Session-Id trailers even with the global hook
# installed. Detect the agent workspace and either warn or chain.
warn_or_chain_local_hooks_override() {
    local workspace=""
    local local_path=""
    local chain="$DOTFILES_DIR/chain-global-hooks.sh"

    if [ -n "${AGENT_WORKSPACE:-}" ] && [ -e "${AGENT_WORKSPACE}/.git" ]; then
        workspace="$AGENT_WORKSPACE"
    elif [ -f "$DOTFILES_DIR/../../gptme.toml" ] && \
         grep -q '\[agent\]' "$DOTFILES_DIR/../../gptme.toml" 2>/dev/null; then
        workspace="$(cd "$DOTFILES_DIR/../.." && pwd)"
    fi
    [ -n "$workspace" ] || return 0

    local_path="$(git -C "$workspace" config --local --get core.hooksPath 2>/dev/null || true)"
    [ -n "$local_path" ] || return 0

    echo -e "${YELLOW}⚠️  $workspace has repo-local core.hooksPath=$local_path${NC}"
    echo "   That overrides the global hooks dir, so Git-Session-Id trailers never run."
    if [ "${CHAIN_HOOKS:-0}" = "1" ]; then
        if [ -x "$chain" ]; then
            echo "   CHAIN_HOOKS=1 — installing wrappers via chain-global-hooks.sh"
            "$chain" "$workspace" || echo -e "${YELLOW}   chain-global-hooks.sh failed (non-fatal)${NC}"
        else
            echo -e "${YELLOW}   chain-global-hooks.sh missing at $chain${NC}"
        fi
    else
        echo "   Fix:  $chain $workspace"
        echo "   Or:   CHAIN_HOOKS=1 $0"
        echo "   Docs: $DOTFILES_DIR/README.md (core.hooksPath override)"
    fi
}

# --- Handle existing hooks directory ---
handle_existing_hooks_dir() {
    local target="$HOME/.config/git/hooks"

    if [ -L "$target" ]; then
        # Existing symlink - remove and recreate
        echo "Removing existing symlink: $target"
        rm "$target"
    elif [ -d "$target" ]; then
        # Existing directory (not symlink) - back up
        local backup
        backup="$target.backup.$(date +%Y%m%d-%H%M%S)"
        echo -e "${YELLOW}⚠️  Existing hooks directory found (not a symlink)${NC}"
        echo "   Backing up to: $backup"
        mv "$target" "$backup"
    elif [ -e "$target" ]; then
        # Something else exists - error
        echo -e "${RED}❌ ERROR: $target exists but is not a directory or symlink${NC}"
        exit 1
    fi
}

# --- Agent identity allowlist for the global pre-commit hook ---
# The pre-commit hook refuses commits from any identity outside its built-in
# defaults unless ~/.config/git/allowed-identities.conf overrides them. A forked
# agent's identity is NOT a built-in default, so without this file every commit
# aborts. Derive the allowlist from the installing agent's git identity and
# leave an existing file untouched (a user may have curated it).
install_allowed_identities() {
    local conf="$HOME/.config/git/allowed-identities.conf"
    local email
    # Only the global identity is authoritative. `git config user.email` without
    # --global resolves the config of whatever repo this script is run from
    # (README: the dotfiles checkout), which is not necessarily the agent's
    # commit identity. A conf that names the wrong identity blocks every commit
    # outright, which is strictly worse than writing none — so never infer it
    # from the cwd's repo-local config.
    email="$(git config --global user.email 2>/dev/null || true)"

    if [ -z "$email" ]; then
        echo -e "${YELLOW}⚠️  git --global user.email is not set — skipping allowed-identities.conf${NC}"
        echo "   The pre-commit hook will refuse every commit until an identity is set."
        echo "   Fix: git config --global user.email you@example.com; then re-run this script."
        echo "   (An identity set only in one repo's local config is not read here — add"
        echo "    it to $conf by hand if you commit with it.)"
        return 0
    fi

    case "$email" in
        bob@superuserlabs.org | timetobuildbob@gmail.com)
            echo -e "${GREEN}✓${NC} Identity $email is covered by the hook's built-in allowlist"
            return 0
            ;;
    esac

    if [ -f "$conf" ]; then
        echo -e "${GREEN}✓${NC} Keeping existing allowed-identities.conf"
        return 0
    fi

    # The file is shell-sourced by the hook, so the identity must be quoted for
    # the shell — a crafted user.email (e.g. from a repo-local override) must not
    # be able to inject commands.
    {
        echo "# Allowed git commit identities for this agent."
        echo "# Sourced by ~/.config/git/hooks/pre-commit, which refuses commits from any"
        echo "# other identity. Overrides (does not extend) the hook's built-in defaults."
        echo "IDENTITY_ALLOWLIST=("
        printf '    %q\n' "$email"
        echo ")"
    } >"$conf"
    echo -e "${GREEN}✓${NC} Wrote allowed-identities.conf for $email"
}

# --- Direct-master-commit allowlist for the global pre-commit/pre-push hooks ---
# Both hooks source ~/.config/git/allowed-repos.conf and, on a repo whose remote
# matches none of ALLOWED_PATTERNS (and which is not an auto-detected agent
# workspace), fail closed — refusing the commit/push. The agent's own workspace
# is auto-allowed via its gptme.toml [agent] section, but the SHARED coordination
# repo (gptme-superuser: standups, cross-agent messages, task updates) is not a
# workspace and is not covered by any built-in default. So without this file a
# fresh fork silently blocks every standup/message commit — and because that
# failure happens *inside* the standup/message automation, the agent looks
# healthy while its coordination commits pile up staged-but-uncommitted.
# Mirror install_allowed_identities: seed the shared repo, never clobber a
# curated file.
install_allowed_repos() {
    local conf="$HOME/.config/git/allowed-repos.conf"

    if [ -f "$conf" ]; then
        echo -e "${GREEN}✓${NC} Keeping existing allowed-repos.conf"
        return 0
    fi

    {
        echo "# Repositories where direct master/main commits and pushes are allowed."
        echo "# Sourced by ~/.config/git/hooks/{pre-commit,pre-push}, which fail closed"
        echo "# (refuse the commit/push) on any repo matching none of these patterns and"
        echo "# not auto-detected as an agent workspace. Patterns are substring-matched"
        echo "# against the full remote URL. Your own workspace is auto-allowed via its"
        echo "# gptme.toml [agent] section, so it does not need listing here."
        echo "ALLOWED_PATTERNS=("
        echo "    # Shared cross-agent coordination repo (standups, messages, task updates)."
        echo "    # Direct master writes are required — a missing standup is a failure signal."
        printf '    %q\n' "gptme/gptme-superuser"
        echo ")"
    } >"$conf"
    echo -e "${GREEN}✓${NC} Wrote allowed-repos.conf (seeded gptme/gptme-superuser)"
}

# --- Main installation ---
echo "Installing dotfiles from $DOTFILES_DIR"
echo ""

# Run safety check
check_agent_environment

# Ensure target directories exist
mkdir -p ~/.config/git

# Handle existing hooks directory
handle_existing_hooks_dir

# Symlink git hooks directory
ln -sf "$DOTFILES_DIR/.config/git/hooks" ~/.config/git/hooks
echo -e "${GREEN}✓${NC} Linked ~/.config/git/hooks -> $DOTFILES_DIR/.config/git/hooks"

# Configure git to use global hooks
git config --global core.hooksPath ~/.config/git/hooks
echo -e "${GREEN}✓${NC} Set core.hooksPath to ~/.config/git/hooks"

# A repo-local core.hooksPath (prek sets .git/hooks) silently disables the
# global dir, so prepare-commit-msg never runs. Warn, and chain when asked.
warn_or_chain_local_hooks_override

# Ensure the hook's identity allowlist covers this agent (no-op for Bob)
install_allowed_identities

# Ensure the shared coordination repo is on the direct-master allowlist
install_allowed_repos

# Set up template directory for pre-commit (create if needed)
mkdir -p ~/.git-templates
git config --global init.templateDir ~/.git-templates
echo -e "${GREEN}✓${NC} Set init.templateDir to ~/.git-templates"

# Use 'simple' push.default so a branch without a matching remote name fails loudly
# instead of silently pushing to whatever upstream the branch tracks.
git config --global push.default simple
echo -e "${GREEN}✓${NC} Set push.default to simple"

echo ""
echo -e "${GREEN}✅ Dotfiles installed successfully!${NC}"
echo ""
echo "Global git hooks are now active:"
echo "  - pre-commit: Branch validation + submodule validation + pre-commit auto-staging"
echo "  - pre-push: Worktree tracking validation"
echo "  - post-checkout: Branch base warning on checkout"
echo "  - prepare-commit-msg: Git-Session-Id trailer (env, AGENT_SESSION_ID, cgroup recovery)"
echo ""
echo "If a repo sets core.hooksPath locally (prek does this), global hooks do not run."
echo "Chain them with: $DOTFILES_DIR/chain-global-hooks.sh /path/to/repo"
echo ""
echo "Seeded allowed-repos.conf (gptme/gptme-superuser) and allowed-identities.conf"
echo "under \$HOME/.config/git/. Add more repos where direct master commits/pushes"
echo "are permitted, or more commit identities, by editing those files."
