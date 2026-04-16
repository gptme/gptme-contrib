#!/usr/bin/env bash
# Agent health check — quick overview of all agent VMs
#
# Usage:
#   ./agent-check.sh                  # check all configured agents
#   ./agent-check.sh bob alice        # check specific agents
#   AGENT_HOSTS="bob alice" ./agent-check.sh
#
# Configuration:
#   Set AGENT_HOSTS env var, or edit the defaults below.
#   Each agent host is an SSH alias (matching ~/.ssh/config).
#
# Per-agent commands:
#   - Claude usage (if check-claude-usage.sh exists)
#   - Session stats via gptme-sessions (auto-discovers CC + gptme sessions)
#   - Recent git activity
#
# Prerequisites:
#   - SSH access to agent VMs (configured in ~/.ssh/config)
#   - gptme-sessions installed on each VM (uv tool install)

set -euo pipefail

# Default agent hosts — override with AGENT_HOSTS env var or positional args
DEFAULT_HOSTS="bob alice gordon"
HOSTS="${AGENT_HOSTS:-$DEFAULT_HOSTS}"
if [[ $# -gt 0 ]]; then
    HOSTS="$*"
fi

STATS_PERIOD="${STATS_PERIOD:-1d}"

for host in $HOSTS; do
    echo "============================================"
    echo "  $host"
    echo "============================================"

    # Detect workspace directory (agent name = host by default)
    workspace="$host"

    # Claude usage (optional — only if script exists)
    ssh "$host" "cd ~/$workspace 2>/dev/null && test -f ./scripts/check-claude-usage.sh && ./scripts/check-claude-usage.sh 2>/dev/null || test -f ./gptme-contrib/scripts/check-claude-usage.sh && ./gptme-contrib/scripts/check-claude-usage.sh 2>/dev/null || echo '  (no claude usage script found)'" 2>/dev/null || echo "  (ssh failed)"

    echo ""

    # Session stats via gptme-sessions
    # First try sync to pick up any new sessions, then show stats
    ssh "$host" "cd ~/$workspace 2>/dev/null && \$HOME/.local/bin/uv tool run gptme-sessions sync --since $STATS_PERIOD --signals 2>/dev/null | tail -1; \$HOME/.local/bin/uv tool run gptme-sessions stats --since $STATS_PERIOD 2>/dev/null || echo '  (gptme-sessions not available)'" 2>/dev/null || echo "  (ssh failed)"

    echo ""

    # Recent git activity
    ssh "$host" "cd ~/$workspace 2>/dev/null && echo 'Recent commits:' && git log --oneline --since '$STATS_PERIOD' 2>/dev/null | head -5 || echo '  (no git repo)'" 2>/dev/null || echo "  (ssh failed)"

    echo ""
done
