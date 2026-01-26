# launchd Support for macOS

This directory contains launchd plist templates for running gptme agents on macOS as an alternative to systemd on Linux.

## Overview

**launchd** is macOS's equivalent of systemd. Instead of `.service` and `.timer` files, launchd uses XML plist files stored in `~/Library/LaunchAgents/`.

## Quick Start

1. Copy the plist files to your LaunchAgents directory:
   ```bash
   cp com.gptme.agent-*.plist ~/Library/LaunchAgents/
   ```

2. Customize the plist files:
   - Update `AGENT_WORKSPACE` path
   - Update `USER` to your username
   - Adjust schedule in `StartCalendarInterval`

3. Load the agents:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.gptme.agent-autonomous.plist
   ```

4. Check status:
   ```bash
   launchctl list | grep gptme
   ```

## Comparison: systemd vs launchd

| systemd | launchd | Description |
|---------|---------|-------------|
| `.service` + `.timer` | Single `.plist` | Service definition |
| `systemctl --user start X` | `launchctl load ~/Library/LaunchAgents/X.plist` | Start service |
| `systemctl --user stop X` | `launchctl unload ~/Library/LaunchAgents/X.plist` | Stop service |
| `systemctl --user status X` | `launchctl list \| grep X` | Check status |
| `journalctl --user -u X` | `cat ~/Library/Logs/gptme-agent/*.log` | View logs |
| `~/.config/systemd/user/` | `~/Library/LaunchAgents/` | Config location |

## Available Templates

| Template | Description | Schedule |
|----------|-------------|----------|
| `com.gptme.agent-autonomous.plist` | Main autonomous run | Weekdays hourly 6am-8pm, weekends 2-hourly |
| `com.gptme.agent-project-monitoring.plist` | GitHub/external monitoring | Every 5 minutes |

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

## Schedule Configuration

launchd uses `StartCalendarInterval` for scheduling:

```xml
<!-- Every hour at minute 0 -->
<key>StartCalendarInterval</key>
<dict>
    <key>Minute</key>
    <integer>0</integer>
</dict>

<!-- Every day at 6am -->
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>6</integer>
    <key>Minute</key>
    <integer>0</integer>
</dict>

<!-- Multiple schedules (array of dicts) -->
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

## Environment Variables

Set environment variables using `EnvironmentVariables`:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>sk-ant-...</string>
</dict>
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

### Permission issues
```bash
# Ensure script is executable
chmod +x /path/to/autonomous-run.sh
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
