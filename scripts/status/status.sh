#!/bin/bash
# Status overview script for agent infrastructure
# Generalized version from Bob's workspace
set -euo pipefail

# Configuration
AGENT_NAME="${AGENT_NAME:-$(basename "$(pwd)")}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors (only define what's used in this script)
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
DIM='\033[2m'
NC='\033[0m'

echo -e "${BLUE}=== ${AGENT_NAME^}'s Infrastructure Status ===${NC}\n"

# Services (from separate script)
echo -e "${YELLOW}Services:${NC}"
"$SCRIPT_DIR/util/status-systemd.sh"
echo ""

# Lock Status (compact with history preview)
if [ -f "$SCRIPT_DIR/util/lock-status.sh" ]; then
    echo -e "${YELLOW}Locks:${NC}"
    lock_output=$("$SCRIPT_DIR/util/lock-status.sh" 2>/dev/null || echo "")
    if [ -n "$lock_output" ]; then
        echo "$lock_output" | grep -E "(LOCKED|STALE)" | sed 's/^/  /' || echo "  (none)"

        # Show recent history (last 3)
        echo ""
        echo -e "  ${DIM}Recent:${NC}"
        echo "$lock_output" | grep -A100 "Recent (last" | tail -n +2 | head -3 | sed 's/^/  /' || true
        echo ""
    else
        echo "  (no lock information available)"
        echo ""
    fi
fi

echo -e "${DIM}More: $SCRIPT_DIR/util/lock-status.sh (for detailed lock info)${NC}"
echo -e "${DIM}Logs: journalctl --user -u ${AGENT_NAME}-<name>.service -o cat --since '1h ago'${NC}"
