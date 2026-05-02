---
name: home-assistant
description: Query Home Assistant for Erik's location, phone state, calendar, and cameras
tags: [home-assistant, iot, context, location, presence]
license: MIT
compatibility: "Requires HA_HOST and HA_API_KEY env vars; HA instance on Erik's local network"
metadata:
  author: bob
  version: "1.0.0"
  tags: "home-assistant,iot,context,location,presence"
  requires_tools: ""
  requires_skills: ""
keywords:
  - "home assistant"
  - "erik location"
  - "is erik home"
  - "ha.py status"
  - "person.erik presence"
---

# Home Assistant Skill

Query Erik's Home Assistant instance for presence, sensor data, calendar events, and cameras.

## Setup

Credentials in `.env` at your agent workspace root:
```
HA_HOST=https://ha.hasselstugan.bjareholt.com
HA_API_KEY=<long-lived-access-token>
```

**Connectivity**: `ha.hasselstugan.bjareholt.com` resolves publicly via Nabu Casa cloud relay
(CNAME → `*.ui.nabu.casa`). No `/etc/hosts` entry or VPN needed.

## Quick Commands

```bash
HA=gptme-contrib/skills/home-assistant/scripts/ha.py

# Test connectivity
uv run python3 $HA status

# Erik's current location / presence
uv run python3 $HA location

# Erik's full real-world context (location, weather, mower, next event, recent automations)
# — useful before voice calls, standups, or scheduling decisions
uv run python3 $HA context
uv run python3 $HA --json context

# All person/device tracker states (Is Erik home? Same location as Tekla?)
uv run python3 $HA persons

# Upcoming calendar events
uv run python3 $HA calendar

# List all cameras
uv run python3 $HA cameras

# Get any entity state
uv run python3 $HA state person.erik

# List all entities (optionally filtered by domain)
uv run python3 $HA states
uv run python3 $HA states --domain sensor
uv run python3 $HA states --domain device_tracker

# JSON output for scripting
uv run python3 $HA location --json
```

## Verified Entity IDs (live as of 2026-05-02)

| What | Entity ID | Notes |
|------|-----------|-------|
| Erik's location | `person.erik` | state: home/away/zone_name |
| Tekla's location | `person.tekla` | state: home/away/zone_name |
| Bob's presence | `person.bob` | state: unknown (not tracked yet) |
| Erik's F8 phone (main) | `device_tracker.erb_f8` | primary GPS source for `person.erik` |
| Erik's F3 phone | `device_tracker.erb_f3_2` | backup tracker |
| Erik's F8 battery | `sensor.erb_f8_battery_level` | %, e.g. 23 |
| Erik's F8 battery state | `sensor.erb_f8_battery_state` | charging/discharging |
| Tekla's phone battery | `sensor.tekla_17t_battery_level` | % |
| Main calendar | `calendar.main` | Erik's main Google Cal |
| gptme internal | `calendar.gptme_internal` | |
| Superuser Labs | `calendar.superuser_labs` | |
| Active camera | `camera.p1435_le_0` | state: idle (accessible) |
| Weather | `weather.forecast_hasselstugan` | e.g. sunny, 5.3°C |
| Lawn mower | `lawn_mower.am310e_nera` | state: mowing/charging/docked |
| Erik's home zone | `zone.home` | "Hasselstugan" |
| `fleet.gptme.ai` outage automation | `automation.fleet_gptme_ai_down` | Erik's HA pings Bob's prod uptime — `last_triggered` = real outage |

## HA → Bob coordination signal

Erik's HA contains an automation `automation.fleet_gptme_ai_down` that fires when
`fleet.gptme.ai` is unreachable. Its `last_triggered` attribute is an
**independent confirmation** of a real production outage — useful when you see
ambiguous signals from PostHog/conformance and want a second opinion. Read it
explicitly:

```bash
uv run python3 gptme-contrib/skills/home-assistant/scripts/ha.py state automation.fleet_gptme_ai_down
```

The `context` subcommand surfaces this automatically under "Automations triggered
in last 24h" alongside other notable events (Tekla arriving home, sauna hot, etc.).

## Other interesting domains discovered (2026-05-02)

The HA instance has 807 entities total. Worth knowing about beyond presence/calendar:

- **`todo.*`** (6 lists: `electricity`, `garden`, `shopping`, `home_automations`,
  `plumbing`, `tekla_projects`) — Erik's HA-managed todos. `state` is item count.
- **`zone.*`** (7 zones: `home`, `baravagen`, `ballet`, `hospital_bmc`,
  `gronegatan`, `eslov_shopping`, `hasselstugan_1km`) — geofenced areas Erik/Tekla
  pass through. `person.erik` resolves to a zone name when not at `home`.
- **`scene.bedtime` / `scene.morning`** — Erik's daily routines.
- **`ai_task.openai_ai_task` / `conversation.openai_conversation`** — HA's own
  OpenAI integration; an alternate path for sending TTS/conversation if the
  Twilio/Realtime path is degraded.
- **`lawn_mower.am310e_nera`** — actively mowing on sunny days; a fun life signal.

## REST API Reference

Base URL: `$HA_HOST/api/`
Auth header: `Authorization: Bearer $HA_API_KEY`

Key endpoints:
- `GET /api/states` — all entity states
- `GET /api/states/<entity_id>` — single entity
- `GET /api/config` — HA config (location, timezone)
- `GET /api/calendars/<entity_id>?start=<iso-utc>&end=<iso-utc>` — calendar events (note: `Z` suffix required for UTC, not `+00:00`)
- `GET /api/camera_proxy/<entity_id>` — camera snapshot (binary JPEG)

## Google Calendar (Already Working)

The agent's Google account should be shared as a reader on the owner's main calendar.
Use `gog` (gogcli) to query it without needing HA:

```bash
# List all accessible calendars
gog calendar calendars --results-only -j

# Upcoming events (Erik's main calendar)
gog calendar events "<owner-google-account>" --results-only -j

# Compact view
gog calendar events "<owner-google-account>" --results-only -j | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', '?'))[:16]
    print(f'{start}  {e.get(\"summary\", \"(no title)\")}')
"
```

Calendar ID: owner's Google account email (alias: "Main", timezone: Europe/Stockholm or as configured)

## Giving Another Agent the Same Capability

### Google Calendar
1. Ask the owner to share their main calendar with the agent's Google account (reader)
2. The other agent can then use `gog calendar events "<owner-google-account>"` once the share is accepted

### Home Assistant
1. SSH to Alice's VM: `ssh alice@alice`
2. Add `HA_HOST` and `HA_API_KEY` to `~/alice/.env`
3. Pull latest gptme-contrib (skill is upstreamed there — `gptme-contrib/skills/home-assistant/`)

## Related

- Issue: https://github.com/ErikBjare/bob/issues/722
- Script: `gptme-contrib/skills/home-assistant/scripts/ha.py`
