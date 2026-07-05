# launchd Support for macOS

This directory contains launchd plist templates for running gptme agents on macOS as an alternative to systemd on Linux.

## Overview

**launchd** is macOS's equivalent of systemd. Instead of `.service` and `.timer` files, launchd uses XML plist files stored in `~/Library/LaunchAgents/`.

## Quick Start

### 1. Set up your workspace

Your agent workspace needs to have the `gptme-runloops` package available:

```bash
# In your agent workspace
cd ~/gptme-agent  # or your workspace path
uv add gptme-runloops
```

### 2. Run the setup script

```bash
./setup-launchd.sh ~/gptme-agent
```

This will:
- Copy plist files to `~/Library/LaunchAgents/`
- Replace placeholder paths with your workspace
- Create log directories
- Show next steps

### 3. Load the agents

```bash
launchctl load ~/Library/LaunchAgents/com.gptme.agent-autonomous.plist
launchctl load ~/Library/LaunchAgents/com.gptme.agent-project-monitoring.plist
```

### 4. Verify

```bash
launchctl list | grep gptme
```

## Available Files

| File | Description |
|------|-------------|
| `autonomous-run.sh` | Script to run a single autonomous session |
| `project-monitoring.sh` | Script to run project monitoring (GitHub, etc.) |
| `com.gptme.agent-autonomous.plist` | Plist template for autonomous runs |
| `com.gptme.agent-project-monitoring.plist` | Plist template for monitoring |
| `setup-launchd.sh` | One-command setup script |

## Scripts

### autonomous-run.sh

Runs a single autonomous session using the `run_loops` CLI:

```bash
# Run with default workspace
./autonomous-run.sh

# Run with custom workspace
./autonomous-run.sh --workspace ~/my-agent

# Or set environment variable
WORKSPACE=~/my-agent ./autonomous-run.sh
```

### project-monitoring.sh

Runs project monitoring (GitHub notifications, PR updates):

```bash
# Run with default workspace
./project-monitoring.sh

# Run with custom workspace
./project-monitoring.sh --workspace ~/my-agent
```

### Requirements

Both scripts require:
1. **uv** installed (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
2. **gptme-runloops** package in your workspace (`uv add gptme-runloops`)
3. **Environment variables** for API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)

## Comparison: systemd vs launchd

| systemd | launchd | Description |
|---------|---------|-------------|
| `.service` + `.timer` | Single `.plist` | Service definition |
| `systemctl --user start X` | `launchctl load ~/Library/LaunchAgents/X.plist` | Start service |
| `systemctl --user stop X` | `launchctl unload ~/Library/LaunchAgents/X.plist` | Stop service |
| `systemctl --user status X` | `launchctl list \| grep X` | Check status |
| `journalctl --user -u X` | `cat ~/Library/Logs/gptme-agent/*.log` | View logs |
| `~/.config/systemd/user/` | `~/Library/LaunchAgents/` | Config location |

## Schedule Configuration

| Template | Default Schedule |
|----------|------------------|
| `com.gptme.agent-autonomous.plist` | Hourly 6am-8pm |
| `com.gptme.agent-project-monitoring.plist` | Every 5 minutes |

### Customizing the schedule

launchd uses `StartCalendarInterval` for time-based scheduling:

```xml
<!-- Every hour at minute 0 -->
<key>StartCalendarInterval</key>
<dict>
    <key>Minute</key>
    <integer>0</integer>
</dict>

<!-- Specific hours (array of dicts) -->
<key>StartCalendarInterval</key>
<array>
    <dict>
        <key>Hour</key><integer>6</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <dict>
        <key>Hour</key><integer>12</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
</array>
```

For interval-based scheduling:

```xml
<!-- Every 5 minutes (300 seconds) -->
<key>StartInterval</key>
<integer>300</integer>
```

## Management Commands

```bash
# Load (start)
launchctl load ~/Library/LaunchAgents/com.gptme.agent-autonomous.plist

# Unload (stop)
launchctl unload ~/Library/LaunchAgents/com.gptme.agent-autonomous.plist

# Run immediately (trigger)
launchctl start com.gptme.agent-autonomous

# List status
launchctl list | grep gptme

# View logs
tail -f ~/Library/Logs/gptme-agent/autonomous.log

# Debug (run interactively)
launchctl debug com.gptme.agent-autonomous
```

## Environment Variables

### Option 1: In plist file

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>sk-ant-...</string>
</dict>
```

### Option 2: In shell profile

The scripts source `~/.profile`, `~/.bash_profile`, and `~/.zshrc`, so you can set environment variables there:

```bash
# In ~/.zshrc or ~/.bash_profile
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
```

## Troubleshooting

### Service not running

```bash
# Check if loaded
launchctl list | grep gptme

# Check for errors
launchctl error <exit_code>

# View system log
log show --predicate 'process == "launchd"' --last 1h
```

### run_loops not found

Ensure the package is installed in your workspace:

```bash
cd ~/gptme-agent
uv add gptme-runloops
```

### Permission issues

```bash
# Ensure scripts are executable
chmod +x scripts/runs/launchd/*.sh
```

### Log location

Logs are configured to go to `~/Library/Logs/gptme-agent/`. Create this directory:

```bash
mkdir -p ~/Library/Logs/gptme-agent
```

## Converting from systemd

When converting a systemd timer to launchd:

1. **Merge service + timer** into single plist
2. **Convert OnCalendar** to `StartCalendarInterval`
3. **Convert WorkingDirectory** to `WorkingDirectory` key
4. **Convert ExecStart** to `ProgramArguments`
5. **Convert TimeoutSec** to `TimeoutSeconds`
6. **Convert StandardOutput/StandardError** to `StandardOutPath`/`StandardErrorPath`

## References

- [launchd.plist man page](https://www.manpagez.com/man/5/launchd.plist/)
- [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- [launchd Tutorial](https://www.launchd.info/)
