#!/bin/bash
# Show lock status for agent runs
# This is optional - only works if agent uses a locking system
set -euo pipefail

AGENT_NAME="${AGENT_NAME:-$(basename "$(dirname "$(dirname "$(dirname "$0")")")")}"
WORKSPACE="${WORKSPACE:-$(pwd)}"
LOCKS_DIR="${LOCKS_DIR:-$WORKSPACE/logs/locks}"

# Check if locks directory exists
if [ ! -d "$LOCKS_DIR" ]; then
    echo "No locks directory found at $LOCKS_DIR"
    exit 0
fi

# Colors
RED='\033[0;31m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

# Find active locks
echo "Active locks:"
lock_found=false
for lock_file in "$LOCKS_DIR"/*.lock; do
    [ -e "$lock_file" ] || continue
    lock_found=true

    lock_name=$(basename "$lock_file" .lock)
    lock_age=$(($(date +%s) - $(stat -c %Y "$lock_file" 2>/dev/null || stat -f %m "$lock_file" 2>/dev/null || echo 0)))

    if [ $lock_age -gt 7200 ]; then  # 2 hours
        echo -e "${RED}STALE${NC}: $lock_name (${lock_age}s old)"
    else
        echo -e "${YELLOW}LOCKED${NC}: $lock_name (${lock_age}s ago)"
    fi
done

if [ "$lock_found" = false ]; then
    echo -e "${DIM}(none)${NC}"
fi

# Show recent history if available
echo ""
echo "Recent (last 5):"
if [ -d "$LOCKS_DIR" ]; then
    ls -lt "$LOCKS_DIR"/*.lock 2>/dev/null | head -5 | awk '{print "  " $9}' | sed 's|.*/||;s|\.lock$||' || echo -e "  ${DIM}(no history)${NC}"
else
    echo -e "  ${DIM}(no history)${NC}"
fi
