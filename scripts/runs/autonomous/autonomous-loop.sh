#!/bin/bash
# Continuously run autonomous sessions until failure or limit reached
#
# This script wraps systemd service invocation for repeated autonomous runs.
# Service name is derived from AGENT_NAME environment variable or config.
#
# Usage:
#   ./autonomous-loop.sh [-n number_of_runs] [-s service_name]
#
# Examples:
#   AGENT_NAME=myagent ./autonomous-loop.sh -n 5   # Run 5 times using myagent-autonomous.service
#   ./autonomous-loop.sh -s custom-autonomous      # Run infinitely using custom-autonomous.service

set -e

# === CONFIGURATION ===
# Service name can be set via:
# 1. -s command line argument
# 2. AGENT_NAME environment variable (becomes ${AGENT_NAME}-autonomous.service)
# 3. Defaults to reading agent.name from gptme.toml if available

get_service_name() {
    local service_name=""
    local agent_name

    # Try to read from gptme.toml in current directory or parent
    if [ -f "gptme.toml" ]; then
        agent_name=$(grep -E '^name\s*=' gptme.toml | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr '[:upper:]' '[:lower:]')
        [ -n "$agent_name" ] && service_name="${agent_name}-autonomous.service"
    elif [ -f "../gptme.toml" ]; then
        agent_name=$(grep -E '^name\s*=' ../gptme.toml | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/' | tr '[:upper:]' '[:lower:]')
        [ -n "$agent_name" ] && service_name="${agent_name}-autonomous.service"
    fi

    # Override with AGENT_NAME env var if set
    if [ -n "$AGENT_NAME" ]; then
        service_name="${AGENT_NAME,,}-autonomous.service"  # lowercase
    fi

    echo "$service_name"
}

# Defaults
counter=0
max_runs=-1  # -1 means infinite
SERVICE_NAME=""

# Parse command line arguments
while getopts "n:s:h" opt; do
    case $opt in
        n)
            max_runs=$OPTARG
            if ! [[ "$max_runs" =~ ^[0-9]+$ ]] || [ "$max_runs" -lt 1 ]; then
                echo "Error: -n parameter must be a positive integer"
                exit 1
            fi
            ;;
        s)
            SERVICE_NAME=$OPTARG
            # Add .service suffix if not present
            [[ "$SERVICE_NAME" != *.service ]] && SERVICE_NAME="${SERVICE_NAME}.service"
            ;;
        h)
            detected=$(get_service_name)
            echo "Usage: $0 [-n number_of_runs] [-s service_name]"
            echo ""
            echo "Options:"
            echo "  -n: Number of runs (default: infinite)"
            echo "  -s: Service name (default: derived from AGENT_NAME or gptme.toml)"
            echo ""
            echo "Service name resolution order:"
            echo "  1. -s command line argument"
            echo "  2. AGENT_NAME environment variable"
            echo "  3. agent.name from gptme.toml"
            echo ""
            if [ -n "$detected" ]; then
                echo "Detected service: $detected"
            else
                echo "Detected service: (none - please provide -s or set AGENT_NAME)"
            fi
            exit 0
            ;;
        \?)
            echo "Usage: $0 [-n number_of_runs] [-s service_name]"
            exit 1
            ;;
    esac
done

# Resolve service name if not explicitly provided
if [ -z "$SERVICE_NAME" ]; then
    SERVICE_NAME=$(get_service_name)
fi

# Validate service name
if [ -z "$SERVICE_NAME" ]; then
    echo "Error: Could not determine service name."
    echo "Please provide via -s flag, AGENT_NAME env var, or gptme.toml"
    exit 1
fi

# Verify service exists
if ! systemctl --user cat "$SERVICE_NAME" &>/dev/null; then
    echo "Warning: Service '$SERVICE_NAME' may not exist or is not accessible"
    echo "Continuing anyway in case it will be created..."
fi

echo "Starting autonomous loop..."
echo "Service: $SERVICE_NAME"
if [ "$max_runs" -eq -1 ]; then
    echo "Running indefinitely (press Ctrl+C to stop)"
else
    echo "Running $max_runs time(s)"
fi
echo "Press Ctrl+C to stop manually"
echo ""

while true; do
    counter=$((counter + 1))

    # Check if we've reached the limit
    if [ "$max_runs" -ne -1 ] && [ "$counter" -gt "$max_runs" ]; then
        echo "✅ Completed all $max_runs runs successfully"
        exit 0
    fi

    # Show progress
    if [ "$max_runs" -eq -1 ]; then
        echo "[$counter] Starting autonomous run at $(date '+%Y-%m-%d %H:%M:%S %Z')..."
    else
        echo "[$counter/$max_runs] Starting autonomous run at $(date '+%Y-%m-%d %H:%M:%S %Z')..."
    fi

    systemctl --user start "$SERVICE_NAME"
    exit_code=$?

    if [ $exit_code -ne 0 ]; then
        echo "❌ ERROR: Autonomous run $counter failed with exit code $exit_code"
        echo "Aborting loop."
        exit $exit_code
    fi

    # Wait for the service to complete
    echo "[$counter] Waiting for service to complete..."
    while systemctl --user is-active --quiet "$SERVICE_NAME"; do
        sleep 5
    done

    echo "✅ Run $counter completed successfully at $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""

    # Short delay between runs to avoid rapid-fire execution
    sleep 10
done
