#!/bin/bash
# Continuously run autonomous sessions until failure or limit reached
#
# Two modes (capability-tiered — the core must not assume systemd):
#   systemd (Tier 1, default when systemctl is present): starts a per-agent
#     ${AGENT_NAME}-autonomous.service and waits for it to go inactive.
#   direct  (Tier 0, fallback when systemctl is absent, or forced with -d):
#     invokes the autonomous run script in-process. This is what lets a bare
#     fork loop on macOS/launchd or a container with no user systemd.
#
# Quality note: alice#50 / bob#725 data shows back-to-back sessions (<30 min gap)
# produce significantly lower quality (mean grade 0.546 vs 0.622 for ≥60 min gaps).
# Default COOLDOWN is 1800s (30 min) to stay out of the low-quality back-to-back regime.
# Override via AGENT_LOOP_COOLDOWN env var for testing/burst scenarios.
#
# Usage:
#   ./autonomous-loop.sh [-n number_of_runs] [-c cooldown] [-s service_name] [-d] [-r run_cmd]
#
# Examples:
#   AGENT_NAME=myagent ./autonomous-loop.sh -n 5   # Run 5 times using myagent-autonomous.service
#   ./autonomous-loop.sh -s custom-autonomous      # Run infinitely using custom-autonomous.service
#   ./autonomous-loop.sh -c 300                    # 5 min cooldown between sessions
#   ./autonomous-loop.sh -d -n 3                   # Tier-0 direct mode: run the run-script 3x
#   AGENT_LOOP_RUN_CMD="./scripts/runs/autonomous/autonomous-run-cc.sh" ./autonomous-loop.sh -d

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

# Direct-mode (Tier 0): resolve the command that runs ONE autonomous session.
# Order: AGENT_LOOP_RUN_CMD env / -r flag (handled by caller) → auto-detect the
# run script relative to cwd, then one level up. Prefer the Claude Code runner,
# fall back to the gptme runner. Echoes empty if nothing is found.
get_run_cmd() {
    local candidate
    for base in "." ".."; do
        for name in autonomous-run-cc.sh autonomous-run.sh; do
            candidate="$base/scripts/runs/autonomous/$name"
            if [ -x "$candidate" ]; then
                echo "$candidate"
                return 0
            fi
        done
    done
    echo ""
}

# Defaults
counter=0
failed_starts=0
max_runs=-1  # -1 means infinite
COOLDOWN="${AGENT_LOOP_COOLDOWN:-1800}"  # seconds between runs
if ! [[ "$COOLDOWN" =~ ^[0-9]+$ ]]; then
    echo "Error: AGENT_LOOP_COOLDOWN must be a non-negative integer (seconds), got: '$COOLDOWN'"
    exit 1
fi
SERVICE_NAME=""
MODE="${AGENT_LOOP_MODE:-}"    # "systemd" | "direct" | "" (auto-detect)
RUN_CMD="${AGENT_LOOP_RUN_CMD:-}"  # direct-mode command to run one session

# Parse command line arguments
while getopts "n:s:c:r:dh" opt; do
    case $opt in
        n)
            max_runs=$OPTARG
            if ! [[ "$max_runs" =~ ^[0-9]+$ ]] || [ "$max_runs" -lt 1 ]; then
                echo "Error: -n parameter must be a positive integer"
                exit 1
            fi
            ;;
        c)
            COOLDOWN=$OPTARG
            if ! [[ "$COOLDOWN" =~ ^[0-9]+$ ]] || [ "$COOLDOWN" -lt 0 ]; then
                echo "Error: -c parameter must be a non-negative integer (seconds)"
                exit 1
            fi
            ;;
        s)
            SERVICE_NAME=$OPTARG
            # Add .service suffix if not present
            [[ "$SERVICE_NAME" != *.service ]] && SERVICE_NAME="${SERVICE_NAME}.service"
            ;;
        d)
            MODE="direct"
            ;;
        r)
            RUN_CMD=$OPTARG
            ;;
        h)
            detected=$(get_service_name)
            echo "Usage: $0 [-n number_of_runs] [-c cooldown_seconds] [-s service_name] [-d] [-r run_cmd]"
            echo ""
            echo "Options:"
            echo "  -n: Number of runs (default: infinite)"
            echo "  -c: Cooldown seconds between runs (default: 1800, override via AGENT_LOOP_COOLDOWN)"
            echo "  -s: Service name (systemd mode; default: derived from AGENT_NAME or gptme.toml)"
            echo "  -d: Force direct mode (Tier 0 — run the run-script in-process, no systemd)"
            echo "  -r: Direct-mode command to run one session (default: auto-detected run script)"
            echo ""
            echo "Mode: systemd when systemctl is present, else direct. Force direct with -d"
            echo "      or AGENT_LOOP_MODE=direct."
            echo ""
            echo "Service name resolution order (systemd mode):"
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
            echo "Usage: $0 [-n number_of_runs] [-c cooldown_seconds] [-s service_name] [-d] [-r run_cmd]"
            exit 1
            ;;
    esac
done

# Auto-detect mode when not forced: systemd if available, else Tier-0 direct.
if [ -z "$MODE" ]; then
    if command -v systemctl >/dev/null 2>&1; then
        MODE="systemd"
    else
        MODE="direct"
    fi
fi

if [ "$MODE" = "systemd" ]; then
    # Resolve service name if not explicitly provided
    if [ -z "$SERVICE_NAME" ]; then
        SERVICE_NAME=$(get_service_name)
    fi

    # Validate service name
    if [ -z "$SERVICE_NAME" ]; then
        echo "Error: Could not determine service name."
        echo "Please provide via -s flag, AGENT_NAME env var, or gptme.toml"
        echo "(Or use -d for Tier-0 direct mode, which needs no systemd service.)"
        exit 1
    fi

    # Verify service exists
    if ! systemctl --user cat "$SERVICE_NAME" &>/dev/null; then
        echo "Warning: Service '$SERVICE_NAME' may not exist or is not accessible"
        echo "Continuing anyway in case it will be created..."
    fi
else
    # Direct mode (Tier 0): resolve the run command.
    if [ -z "$RUN_CMD" ]; then
        RUN_CMD=$(get_run_cmd)
    fi
    if [ -z "$RUN_CMD" ]; then
        echo "Error: direct mode could not find an autonomous run script."
        echo "Provide one via -r <cmd> or AGENT_LOOP_RUN_CMD, or run from a"
        echo "workspace containing scripts/runs/autonomous/autonomous-run{,-cc}.sh"
        exit 1
    fi
fi

echo "Starting autonomous loop..."
if [ "$MODE" = "systemd" ]; then
    echo "Mode: systemd — service: $SERVICE_NAME"
else
    echo "Mode: direct — run command: $RUN_CMD"
fi
if [ "$max_runs" -eq -1 ]; then
    echo "Running indefinitely (press Ctrl+C to stop)"
else
    echo "Running $max_runs time(s)"
fi
echo "Cooldown: ${COOLDOWN}s between runs"
echo "Press Ctrl+C to stop manually"
echo ""

while true; do
    counter=$((counter + 1))

    # Check if we've reached the limit
    if [ "$max_runs" -ne -1 ] && [ "$counter" -gt "$max_runs" ]; then
        if [ "$failed_starts" -gt 0 ]; then
            echo "⚠️  Completed $max_runs runs ($failed_starts failed to start)"
        else
            echo "✅ Completed all $max_runs runs successfully"
        fi
        exit 0
    fi

    # Show progress
    if [ "$max_runs" -eq -1 ]; then
        echo "[$counter] Starting autonomous run at $(date '+%Y-%m-%d %H:%M:%S %Z')..."
    else
        echo "[$counter/$max_runs] Starting autonomous run at $(date '+%Y-%m-%d %H:%M:%S %Z')..."
    fi

    # Don't abort on a single run failure (e.g. transient lock contention,
    # pre-start gate rejection). Log and continue so the loop stays resilient
    # and the wrapper doesn't have to be bounced for every hiccup.
    if [ "$MODE" = "systemd" ]; then
        if systemctl --user start "$SERVICE_NAME"; then
            # Wait for the service to complete
            echo "[$counter] Waiting for service to complete..."
            while systemctl --user is-active --quiet "$SERVICE_NAME"; do
                sleep 5
            done
            echo "✅ Run $counter completed successfully at $(date '+%Y-%m-%d %H:%M:%S %Z')"
        else
            failed_starts=$((failed_starts + 1))
            echo "⚠️  Run $counter failed to start — continuing loop after cooldown"
        fi
    else
        # Direct mode: run one session in-process. The run script owns its own
        # gates/locking; a non-zero exit is one failed session, not a reason to
        # abort the loop, so temporarily relax `set -e` around it.
        set +e
        bash -c "$RUN_CMD"
        run_rc=$?
        set -e
        if [ "$run_rc" -eq 0 ]; then
            echo "✅ Run $counter completed successfully at $(date '+%Y-%m-%d %H:%M:%S %Z')"
        else
            failed_starts=$((failed_starts + 1))
            echo "⚠️  Run $counter exited $run_rc — continuing loop after cooldown"
        fi
    fi
    echo ""

    # Cooldown between runs
    if [ "$COOLDOWN" -gt 0 ]; then
        echo "Cooling down for ${COOLDOWN}s..."
        sleep "$COOLDOWN"
    fi
done
