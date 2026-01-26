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

# Check for run_loops availability
if command -v uv &>/dev/null; then
    echo -e "${YELLOW}Checking run_loops package...${NC}"
    if ! (cd "$AGENT_WORKSPACE" && uv run python3 -c "import run_loops" 2>/dev/null); then
        echo -e "${YELLOW}Warning: run_loops package not found in workspace.${NC}"
        echo "  Install with: cd $AGENT_WORKSPACE && uv add gptme-runloops"
        echo ""
    else
        echo -e "  ${GREEN}✓ run_loops package found${NC}"
    fi
fi

# Create directories
echo -e "${YELLOW}Creating directories...${NC}"
mkdir -p "$LAUNCHD_DIR"
mkdir -p "$LOG_DIR"
echo "  Created: $LAUNCHD_DIR"
echo "  Created: $LOG_DIR"

# Copy scripts to workspace if not already there
echo -e "${YELLOW}Copying run scripts to workspace...${NC}"
SCRIPTS_DEST="$AGENT_WORKSPACE/scripts/runs/launchd"
mkdir -p "$SCRIPTS_DEST"

for script in autonomous-run.sh project-monitoring.sh; do
    if [[ -f "$SCRIPT_DIR/$script" ]]; then
        cp "$SCRIPT_DIR/$script" "$SCRIPTS_DEST/"
        chmod +x "$SCRIPTS_DEST/$script"
        echo "  Copied: $script"
    fi
done

# Function to install plist
install_plist() {
    local template="$1"
    local target
    target="$LAUNCHD_DIR/$(basename "$template")"

    echo -e "${YELLOW}Installing: $(basename "$template")${NC}"

    # Copy and customize plist with all placeholders
    sed -e "s|AGENT_WORKSPACE|$AGENT_WORKSPACE|g" \
        -e "s|USER_HOME|$HOME|g" \
        -e "s|/Users/YOUR_USERNAME|$HOME|g" \
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
echo "1. Ensure API keys are available (in shell profile or plist):"
echo "   export ANTHROPIC_API_KEY='sk-ant-...'"
echo "   export OPENAI_API_KEY='sk-...'"
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
