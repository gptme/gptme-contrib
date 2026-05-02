---
name: home-assistant
description: Query a Home Assistant instance for presence, sensor data, calendar events, and cameras
tags: [home-assistant, iot, context, location, presence]
license: MIT
compatibility: "Requires HA_HOST and HA_API_KEY env vars"
metadata:
  author: bob
  version: "1.0.0"
  tags: "home-assistant,iot,context,location,presence"
  requires_tools: ""
  requires_skills: ""
keywords:
  - "home assistant"
  - "is person home"
  - "ha.py status"
  - "person presence"
  - "home assistant sensor"
---

# Home Assistant Skill

Query a Home Assistant instance for presence, sensor data, calendar events, and cameras.

## Setup

Credentials in `.env` at your agent workspace root:
```
HA_HOST=https://<your-ha-host>
HA_API_KEY=<long-lived-access-token>
```

**Cloud connectivity**: If your HA instance uses Nabu Casa, the host resolves publicly
(CNAME → `*.ui.nabu.casa`). No `/etc/hosts` entry or VPN needed.

**Generate a long-lived token**: HA UI → Profile → Long-Lived Access Tokens → Create.

## Quick Commands

```bash
HA=gptme-contrib/skills/home-assistant/scripts/ha.py

# Test connectivity
uv run python3 $HA status

# Owner's current location / presence
uv run python3 $HA location

# Full real-world context (location, weather, next event, recent automations)
# — useful before voice calls, standups, or scheduling decisions
uv run python3 $HA context
uv run python3 $HA --json context

# All person/device tracker states
uv run python3 $HA persons

# Upcoming calendar events
uv run python3 $HA calendar

# List all cameras
uv run python3 $HA cameras

# Get any entity state
uv run python3 $HA state <entity_id>

# List all entities (optionally filtered by domain)
uv run python3 $HA states
uv run python3 $HA states --domain sensor
uv run python3 $HA states --domain device_tracker

# JSON output for scripting
uv run python3 $HA location --json
```

## Common Entity ID Patterns

HA entity IDs follow predictable patterns — discover yours with `ha.py states`:

| Domain | Example | Notes |
|--------|---------|-------|
| `person.<name>` | `person.alice` | state: home/away/zone_name |
| `device_tracker.*` | `device_tracker.alice_phone` | GPS source for person entity |
| `sensor.*_battery_level` | `sensor.alice_phone_battery_level` | % |
| `calendar.main` | `calendar.main` | Primary Google calendar |
| `weather.forecast_*` | `weather.forecast_home` | Current weather |
| `camera.*` | `camera.front_door` | Camera snapshot |
| `zone.home` | `zone.home` | Home geofence |

Use `ha.py states --domain sensor` to browse what's available on your instance.

## REST API Reference

Base URL: `$HA_HOST/api/`
Auth header: `Authorization: Bearer $HA_API_KEY`

Key endpoints:
- `GET /api/states` — all entity states
- `GET /api/states/<entity_id>` — single entity
- `GET /api/config` — HA config (location, timezone)
- `GET /api/calendars/<entity_id>?start=<iso-utc>&end=<iso-utc>` — calendar events (note: `Z` suffix required for UTC, not `+00:00`)
- `GET /api/camera_proxy/<entity_id>` — camera snapshot (binary JPEG)

## Google Calendar (Alternative)

If the agent's Google account is shared as a reader on the owner's main calendar,
use `gog` (gogcli) directly without HA:

```bash
# Upcoming events
gog calendar events "<owner-google-account>" --results-only -j

# Compact view
gog calendar events "<owner-google-account>" --results-only -j | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', '?'))[:16]
    print(f'{start}  {e.get(\"summary\", \"(no title)\")}')
"
```

## Giving Another Agent the Same Capability

1. SSH to the other agent's VM
2. Add `HA_HOST` and `HA_API_KEY` to their `.env`
3. Pull latest gptme-contrib — the script is at `gptme-contrib/skills/home-assistant/scripts/ha.py`

For Google Calendar: share the owner's calendar with the agent's Google account (reader access).
