#!/bin/bash
# Show systemd service status for agent services
# For use with gptme agents
# Usage: ./scripts/status/util/status-systemd.sh [--no-header] [--no-color]
set -euo pipefail

# Configuration
# Detect agent name from:
# 1. AGENT_NAME environment variable
# 2. gptme.toml in workspace (search up from script location)
# 3. Fall back to current directory name
detect_agent_name() {
    # Try to find gptme.toml by searching up from current directory
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/gptme.toml" ]]; then
            # Extract agent name from gptme.toml
            local name
            name=$(grep -E '^\s*name\s*=' "$dir/gptme.toml" 2>/dev/null | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr '[:upper:]' '[:lower:]')
            if [[ -n "$name" ]]; then
                echo "$name"
                return 0
            fi
        fi
        dir=$(dirname "$dir")
    done

    # Fall back to current directory name
    basename "$PWD"
}

AGENT_NAME="${AGENT_NAME:-$(detect_agent_name)}"

# Colors (can be disabled)
if [[ "$*" == *"--no-color"* ]]; then
    GREEN='' RED='' CYAN='' DIM='' NC=''
else
    GREEN='\033[0;32m' RED='\033[0;31m'
    CYAN='\033[0;36m' DIM='\033[2m' NC='\033[0m'
fi

SHOW_HEADER=true
[[ "$*" == *"--no-header"* ]] && SHOW_HEADER=false

# Show header if enabled
if [[ "$SHOW_HEADER" == true ]]; then
    echo -e "${CYAN}Systemd Services (${AGENT_NAME}):${NC}"
fi

# Get timer data
timer_json=$(systemctl list-timers --user --no-pager --all --output=json 2>/dev/null)

# Get exit codes for all agent services
declare -A exit_codes
current_status=""
while IFS= read -r line; do
    if [[ "$line" =~ ^ExecMainStatus=(.+)$ ]]; then
        current_status="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^Id=(.+)$ ]] && [[ -n "$current_status" ]]; then
        exit_codes["${BASH_REMATCH[1]}"]="$current_status"
        current_status=""
    fi
done < <(systemctl --user show "${AGENT_NAME}-*.service" --property=Id,ExecMainStatus 2>/dev/null)

# Get all agent services and their states
while IFS= read -r line; do
    [[ -z "$line" ]] && continue

    # Parse service name and state (column 3 is ACTIVE state)
    service_name=$(echo "$line" | awk '{print $1}')
    state=$(echo "$line" | awk '{print $3}')

    # Skip if not our agent's service
    [[ ! "$service_name" =~ ^${AGENT_NAME}- ]] && continue

    # Extract short name (remove agent prefix and .service suffix)
    short_name="${service_name#${AGENT_NAME}-}"
    short_name="${short_name%.service}"

    # Get exit code (with default if not found)
    exit_code="${exit_codes[$service_name]:-0}"

    # Determine status icon and color
    if [[ "$state" == "active" ]]; then
        status_icon="${GREEN}●${NC}"
        status_text="${GREEN}active${NC}"
    elif [[ "$state" == "inactive" ]]; then
        if [[ "$exit_code" == "0" ]]; then
            status_icon="${DIM}○${NC}"
            status_text="${DIM}inactive${NC}"
        else
            status_icon="${RED}●${NC}"
            status_text="${RED}failed (exit $exit_code)${NC}"
        fi
    elif [[ "$state" == "failed" ]]; then
        status_icon="${RED}✗${NC}"
        status_text="${RED}failed (exit $exit_code)${NC}"
    else
        status_icon="?"
        status_text="$state"
    fi

    # Check for timer
    timer_name="${service_name%.service}.timer"
    timer_info=""
    if systemctl --user is-enabled "$timer_name" &>/dev/null; then
        # Extract next trigger from timer JSON (timestamp in microseconds)
        next_trigger=$(echo "$timer_json" | jq -r ".[] | select(.unit == \"$timer_name\") | .next" 2>/dev/null || echo "")
        if [[ -n "$next_trigger" && "$next_trigger" != "null" && "$next_trigger" != "n/a" && "$next_trigger" != "0" ]]; then
            # Convert from microseconds to seconds
            next_epoch=$((next_trigger / 1000000))
            now_epoch=$(date +%s)
            seconds_until=$((next_epoch - now_epoch))

            if [ $seconds_until -lt 60 ]; then
                timer_info=" ${DIM}(next: ${seconds_until}s)${NC}"
            elif [ $seconds_until -lt 3600 ]; then
                timer_info=" ${DIM}(next: $((seconds_until / 60))m)${NC}"
            else
                timer_info=" ${DIM}(next: $((seconds_until / 3600))h)${NC}"
            fi
        fi
    fi

    echo -e "  $status_icon $short_name: $status_text$timer_info"

done < <(systemctl --user list-units "${AGENT_NAME}-*.service" --all --no-legend --no-pager 2>/dev/null)

# If no services found
if ! systemctl --user list-units "${AGENT_NAME}-*.service" --all --no-legend --no-pager 2>/dev/null | grep -q .; then
    echo -e "  ${DIM}(no ${AGENT_NAME} services found)${NC}"
fi
