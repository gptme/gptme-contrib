# Agent Status Scripts

Infrastructure status monitoring scripts for gptme agents. Shows systemd service status, locks, and recent activity.

## Overview

These scripts provide a quick overview of your agent's infrastructure health:
- **Systemd services**: Status of timers and services (active, failed, next run)
- **Lock status**: Active locks and recent history (optional)
- **Recent activity**: Quick access to logs and history

Works with any gptme agent workspace.

## Quick Start

```bash
# From your agent workspace
./scripts/status/status.sh

# Or set agent name explicitly
AGENT_NAME=thomas ./scripts/status/status.sh
```

## Requirements

- systemd user services configured (for service monitoring)
- jq (for JSON parsing)
- Systemd services named with pattern: `{agent-name}-{service-name}.service`
- Optional: locks directory at `logs/locks/` (for lock monitoring)

## Configuration

The scripts use environment variables for configuration:

- `AGENT_NAME`: Name of your agent (default: current directory name)
- `WORKSPACE`: Agent workspace path (default: current directory)
- `LOCKS_DIR`: Lock files directory (default: `$WORKSPACE/logs/locks`)

## Usage Examples

### Basic usage
```bash
# Show status for current agent
cd ~/thomas
./scripts/status/status.sh
```

### With explicit configuration
```bash
# Show status for specific agent
AGENT_NAME=myagent WORKSPACE=/home/myagent/myagent ./scripts/status/status.sh
```

### Individual utilities
```bash
# Just systemd status
./scripts/status/util/status-systemd.sh

# Just lock status
./scripts/status/util/lock-status.sh

# Without colors (for logs/automation)
./scripts/status/util/status-systemd.sh --no-color
```

## Expected Output

```text
=== Your-Agent's Infrastructure Status ===

Services:
Systemd Services (your-agent):
  ● autonomous: inactive (exit 0) (next: 1h)
  ● discord: active
  ○ email: inactive

Locks:
  (none)

More: ./scripts/status/util/lock-status.sh (for detailed lock info)
Logs: journalctl --user -u your-agent-<name>.service -o cat --since '1h ago'
```
