#!/bin/bash
# Setup script for gptme agent launchd jobs on macOS
# Usage: ./setup-launchd.sh [AGENT_WORKSPACE]

set -e

# Configuration
AGENT_WORKSPACE="${1:-$HOME/gptme-agent}"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/gptme-agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}gptme Agent launchd Setup${NC}"
echo "================================"
echo "Agent workspace: $AGENT_WORKSPACE"
echo "LaunchAgents dir: $LAUNCHD_DIR"
echo ""

# Check if macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo -e "${RED}Error: This script is for macOS only.${NC}"
    echo "For Linux, use systemd. See: scripts/runs/systemd/"
    exit 1
fi

# Check workspace exists
if [[ ! -d "$AGENT_WORKSPACE" ]]; then
    echo -e "${RED}Error: Agent workspace not found: $AGENT_WORKSPACE${NC}"
    echo "Usage: $0 /path/to/your/agent/workspace"
    exit 1
fi

# Create directories
echo -e "${YELLOW}Creating directories...${NC}"
mkdir -p "$LAUNCHD_DIR"
mkdir -p "$LOG_DIR"
echo "  Created: $LAUNCHD_DIR"
echo "  Created: $LOG_DIR"

# Function to install plist
install_plist() {
    local template="$1"
    local target="$LAUNCHD_DIR/$(basename "$template")"
    
    echo -e "${YELLOW}Installing: $(basename "$template")${NC}"
    
    # Copy and customize plist
    sed -e "s|/Users/YOUR_USERNAME|$HOME|g" \
        -e "s|\$HOME/gptme-agent|$AGENT_WORKSPACE|g" \
        "$template" > "$target"
    
    echo "  Installed: $target"
}

# Install plist files
for plist in "$SCRIPT_DIR"/*.plist; do
    if [[ -f "$plist" ]]; then
        install_plist "$plist"
    fi
done

echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo ""
echo "1. Edit API keys in the plist files (or ensure they're in your .env file):"
echo "   open $LAUNCHD_DIR"
echo ""
echo "2. Load the agents:"
echo "   launchctl load ~/Library/LaunchAgents/com.gptme.agent-autonomous.plist"
echo "   launchctl load ~/Library/LaunchAgents/com.gptme.agent-project-monitoring.plist"
echo ""
echo "3. Verify they're loaded:"
echo "   launchctl list | grep gptme"
echo ""
echo "4. To trigger a run immediately:"
echo "   launchctl start com.gptme.agent-autonomous"
echo ""
echo "5. View logs:"
echo "   tail -f ~/Library/Logs/gptme-agent/autonomous.log"
echo ""
echo "To uninstall:"
echo "   launchctl unload ~/Library/LaunchAgents/com.gptme.agent-*.plist"
echo "   rm ~/Library/LaunchAgents/com.gptme.agent-*.plist"
