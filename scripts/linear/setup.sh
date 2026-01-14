#!/bin/bash
set -e

# Linear Integration Setup Script
# This script sets up the Linear agent integration with all necessary configuration

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print functions
info() { echo -e "${BLUE}ℹ${NC} $1"; }
success() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }
header() { echo -e "\n${BLUE}═══════════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}\n"; }

# Check if running on Linux
check_linux() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        error "This script only supports Linux with systemd."
        error "Detected OS: $(uname -s)"
        exit 1
    fi

    # Check for systemd
    if ! command -v systemctl &> /dev/null; then
        error "systemd is required but not found."
        exit 1
    fi

    success "Running on Linux with systemd"
}

# Check prerequisites
check_prerequisites() {
    header "Checking Prerequisites"

    local missing=()

    # Check Python 3.10+
    if command -v python3 &> /dev/null; then
        local py_version py_major py_minor
        py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        py_major=$(echo "$py_version" | cut -d. -f1)
        py_minor=$(echo "$py_version" | cut -d. -f2)
        if [[ $py_major -ge 3 && $py_minor -ge 10 ]]; then
            success "Python $py_version found"
        else
            missing+=("Python 3.10+ (found $py_version)")
        fi
    else
        missing+=("Python 3.10+")
    fi

    # Check uv
    if command -v uv &> /dev/null; then
        success "uv found"
    else
        missing+=("uv (install: curl -LsSf https://astral.sh/uv/install.sh | sh)")
    fi

    # Check ngrok
    if command -v ngrok &> /dev/null; then
        success "ngrok found"
    else
        missing+=("ngrok (install: snap install ngrok or see https://ngrok.com/download)")
    fi

    # Check gptme
    if command -v gptme &> /dev/null; then
        success "gptme found"
    else
        missing+=("gptme (install: pipx install gptme)")
    fi

    # Check git
    if command -v git &> /dev/null; then
        success "git found"
    else
        missing+=("git")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing prerequisites:"
        for item in "${missing[@]}"; do
            echo "  - $item"
        done
        exit 1
    fi

    success "All prerequisites met"
}

# Prompt for input with default value
prompt() {
    local prompt_text="$1"
    local default="$2"
    local var_name="$3"

    if [[ -n "$default" ]]; then
        read -p "$prompt_text [$default]: " value
        value="${value:-$default}"
    else
        read -p "$prompt_text: " value
    fi

    eval "$var_name='$value'"
}

# Prompt for secret (no echo)
prompt_secret() {
    local prompt_text="$1"
    local var_name="$2"

    read -sp "$prompt_text: " value
    echo
    eval "$var_name='$value'"
}

# Validate non-empty
validate_required() {
    local value="$1"
    local name="$2"

    if [[ -z "$value" ]]; then
        error "$name is required"
        exit 1
    fi
}

# Main setup function
main() {
    header "Linear Agent Integration Setup"

    echo "This script will set up Linear integration for your gptme-based agent."
    echo "You will need:"
    echo "  - Linear OAuth application credentials (from Linear settings)"
    echo "  - ngrok installed and authenticated"
    echo ""
    read -p "Press Enter to continue or Ctrl+C to cancel..."

    # Check environment
    check_linux
    check_prerequisites

    # Get agent configuration
    header "Agent Configuration"

    prompt "Agent name (used for @mentions and service names)" "" AGENT_NAME
    validate_required "$AGENT_NAME" "Agent name"

    local default_workspace="$HOME/repos/$AGENT_NAME"
    prompt "Agent workspace path" "$default_workspace" AGENT_WORKSPACE
    validate_required "$AGENT_WORKSPACE" "Workspace path"

    # Expand ~ if present
    AGENT_WORKSPACE="${AGENT_WORKSPACE/#\~/$HOME}"

    if [[ ! -d "$AGENT_WORKSPACE" ]]; then
        error "Workspace directory does not exist: $AGENT_WORKSPACE"
        exit 1
    fi

    local default_worktree="$HOME/repos/${AGENT_NAME}-worktrees"
    prompt "Worktree base path (for session isolation)" "$default_worktree" WORKTREE_BASE
    WORKTREE_BASE="${WORKTREE_BASE/#\~/$HOME}"

    prompt "Default git branch" "main" DEFAULT_BRANCH

    # Get ngrok configuration
    header "ngrok Configuration"

    echo "You need a public HTTPS URL for Linear webhooks."
    echo ""
    echo "If you don't have ngrok running yet, open another terminal and run:"
    echo "  ngrok http 8081"
    echo ""
    echo "Then copy the Forwarding URL (e.g., https://abc123.ngrok-free.app)"
    echo ""

    prompt "ngrok URL (without trailing slash)" "" NGROK_URL
    validate_required "$NGROK_URL" "ngrok URL"

    # Remove trailing slash if present
    NGROK_URL="${NGROK_URL%/}"

    # Calculate callback URLs
    WEBHOOK_URL="${NGROK_URL}/webhook"
    CALLBACK_URL="${NGROK_URL}/oauth/callback"

    # Display URLs for Linear setup
    header "Linear OAuth Application Setup"

    echo "Now you need to create an OAuth application in Linear."
    echo ""
    echo "1. Go to Linear → Settings → API → OAuth Applications"
    echo "2. Click '+' to create new application"
    echo "3. Fill in these values:"
    echo ""
    echo -e "   ${GREEN}Name:${NC}              $AGENT_NAME"
    echo -e "   ${GREEN}Webhook URL:${NC}       $WEBHOOK_URL"
    echo -e "   ${GREEN}OAuth Callback URL:${NC} $CALLBACK_URL"
    echo ""
    echo "4. Enable 'Webhooks' toggle"
    echo "5. Check these webhook events:"
    echo "   ✓ Agent session events (REQUIRED)"
    echo "   ✓ Inbox notifications (optional)"
    echo ""
    echo "6. Click 'Create'"
    echo ""
    echo "7. Copy the credentials shown on the next screen"
    echo ""
    read -p "Press Enter when you have the credentials..."

    # Get Linear credentials
    header "Linear Credentials"

    echo "Enter the credentials from your Linear OAuth application:"
    echo ""

    prompt_secret "Client ID" LINEAR_CLIENT_ID
    validate_required "$LINEAR_CLIENT_ID" "Client ID"

    prompt_secret "Client Secret" LINEAR_CLIENT_SECRET
    validate_required "$LINEAR_CLIENT_SECRET" "Client Secret"

    prompt_secret "Webhook Secret" LINEAR_WEBHOOK_SECRET
    validate_required "$LINEAR_WEBHOOK_SECRET" "Webhook Secret"

    # Server port
    prompt "Webhook server port" "8081" PORT

    # Create directories
    header "Creating Directories"

    mkdir -p "$WORKTREE_BASE"
    success "Created worktree base: $WORKTREE_BASE"

    mkdir -p "$AGENT_WORKSPACE/logs/linear-sessions"
    success "Created logs directory: $AGENT_WORKSPACE/logs/linear-sessions"

    mkdir -p "$AGENT_WORKSPACE/logs/linear-notifications"
    success "Created notifications directory: $AGENT_WORKSPACE/logs/linear-notifications"

    local LINEAR_DIR="$AGENT_WORKSPACE/scripts/linear"
    mkdir -p "$LINEAR_DIR"
    success "Created linear scripts directory: $LINEAR_DIR"

    # Copy scripts if not already present
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ "$SCRIPT_DIR" != "$LINEAR_DIR" ]]; then
        cp "$SCRIPT_DIR/linear-webhook-server.py" "$LINEAR_DIR/" 2>/dev/null || true
        cp "$SCRIPT_DIR/linear-activity.py" "$LINEAR_DIR/" 2>/dev/null || true
        success "Copied linear scripts to workspace"
    fi

    # Create .env file
    header "Creating Configuration"

    cat > "$LINEAR_DIR/.env" << EOF
# Linear Integration Configuration
# Generated by setup.sh on $(date -Iseconds)

# Linear OAuth Application credentials
LINEAR_WEBHOOK_SECRET=$LINEAR_WEBHOOK_SECRET
LINEAR_CLIENT_ID=$LINEAR_CLIENT_ID
LINEAR_CLIENT_SECRET=$LINEAR_CLIENT_SECRET

# OAuth callback URL (for token refresh)
LINEAR_CALLBACK_URL=$CALLBACK_URL

# Agent configuration
AGENT_NAME=$AGENT_NAME
AGENT_WORKSPACE=$AGENT_WORKSPACE
DEFAULT_BRANCH=$DEFAULT_BRANCH

# Optional: Override derived paths
WORKTREE_BASE=$WORKTREE_BASE
NOTIFICATIONS_DIR=$AGENT_WORKSPACE/logs/linear-notifications

# Server configuration
PORT=$PORT
EOF

    chmod 600 "$LINEAR_DIR/.env"
    success "Created .env file: $LINEAR_DIR/.env"

    # Add .env to .gitignore if not present
    local GITIGNORE="$AGENT_WORKSPACE/.gitignore"
    if [[ -f "$GITIGNORE" ]]; then
        if ! grep -q "scripts/linear/.env" "$GITIGNORE" 2>/dev/null; then
            echo "scripts/linear/.env" >> "$GITIGNORE"
            echo "scripts/linear/.tokens.json" >> "$GITIGNORE"
            success "Added secrets to .gitignore"
        fi
    fi

    # Create systemd services
    header "Setting Up Systemd Services"

    mkdir -p ~/.config/systemd/user

    # Webhook service
    cat > ~/.config/systemd/user/${AGENT_NAME}-linear-webhook.service << EOF
[Unit]
Description=${AGENT_NAME^} Linear Webhook Server - Handle Linear agent sessions
After=network.target

[Service]
Type=simple
WorkingDirectory=$LINEAR_DIR
ExecStart=/usr/bin/env uv run $LINEAR_DIR/linear-webhook-server.py
Restart=always
RestartSec=5
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
EOF
    success "Created webhook service: ${AGENT_NAME}-linear-webhook.service"

    # ngrok service
    cat > ~/.config/systemd/user/${AGENT_NAME}-ngrok.service << EOF
[Unit]
Description=Ngrok tunnel for ${AGENT_NAME^} Linear webhook
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/env ngrok http $PORT --log=stdout
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    success "Created ngrok service: ${AGENT_NAME}-ngrok.service"

    # Reload systemd
    systemctl --user daemon-reload
    success "Reloaded systemd daemon"

    # Enable services
    systemctl --user enable ${AGENT_NAME}-linear-webhook.service
    systemctl --user enable ${AGENT_NAME}-ngrok.service
    success "Enabled services to start on boot"

    # OAuth flow
    header "OAuth Authorization"

    echo "You need to complete the OAuth flow to get access tokens."
    echo ""
    echo "Run the following command:"
    echo ""
    echo -e "  ${GREEN}cd $LINEAR_DIR && uv run linear-activity.py auth${NC}"
    echo ""
    echo "This will open a browser to authorize the application."
    echo "After authorization, tokens will be saved to .tokens.json"
    echo ""

    read -p "Would you like to run the OAuth flow now? [Y/n]: " run_oauth
    run_oauth="${run_oauth:-Y}"

    if [[ "$run_oauth" =~ ^[Yy] ]]; then
        cd "$LINEAR_DIR"
        uv run linear-activity.py auth || {
            warn "OAuth flow failed. You can retry manually later."
        }
    fi

    # Start services
    header "Starting Services"

    read -p "Would you like to start the services now? [Y/n]: " start_services
    start_services="${start_services:-Y}"

    if [[ "$start_services" =~ ^[Yy] ]]; then
        systemctl --user start ${AGENT_NAME}-ngrok.service
        sleep 2
        systemctl --user start ${AGENT_NAME}-linear-webhook.service

        echo ""
        info "Service status:"
        systemctl --user status ${AGENT_NAME}-ngrok.service --no-pager || true
        echo ""
        systemctl --user status ${AGENT_NAME}-linear-webhook.service --no-pager || true
    fi

    # Final instructions
    header "Setup Complete!"

    echo -e "${GREEN}Linear integration has been set up successfully!${NC}"
    echo ""
    echo "Configuration summary:"
    echo "  Agent name:     $AGENT_NAME"
    echo "  Workspace:      $AGENT_WORKSPACE"
    echo "  Webhook URL:    $WEBHOOK_URL"
    echo "  Callback URL:   $CALLBACK_URL"
    echo ""
    echo "Service management:"
    echo "  Start:   systemctl --user start ${AGENT_NAME}-linear-webhook ${AGENT_NAME}-ngrok"
    echo "  Stop:    systemctl --user stop ${AGENT_NAME}-linear-webhook ${AGENT_NAME}-ngrok"
    echo "  Status:  systemctl --user status ${AGENT_NAME}-linear-webhook ${AGENT_NAME}-ngrok"
    echo "  Logs:    journalctl --user -u ${AGENT_NAME}-linear-webhook -f"
    echo ""
    echo -e "${YELLOW}Important:${NC}"
    echo "  1. Ask your admin to run: sudo loginctl enable-linger $USER"
    echo "     (This ensures services start on boot)"
    echo ""
    echo "  2. If using free ngrok, the URL changes on restart."
    echo "     Update the Webhook URL in Linear settings if needed."
    echo ""
    echo "  3. Test by @mentioning @${AGENT_NAME} in a Linear issue!"
    echo ""
}

# Run main function
main "$@"
