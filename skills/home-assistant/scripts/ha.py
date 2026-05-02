#!/usr/bin/env python3
"""Home Assistant CLI — query your HA instance for presence, sensors, calendar, cameras.

Usage:
    uv run python3 skills/home-assistant/scripts/ha.py status              # Test connectivity
    uv run python3 skills/home-assistant/scripts/ha.py location            # Person/tracker states
    uv run python3 skills/home-assistant/scripts/ha.py persons             # All person entity states
    uv run python3 skills/home-assistant/scripts/ha.py calendar            # Upcoming calendar events
    uv run python3 skills/home-assistant/scripts/ha.py cameras             # List cameras (+ save snapshots)
    uv run python3 skills/home-assistant/scripts/ha.py states              # All entity states
    uv run python3 skills/home-assistant/scripts/ha.py states --domain sensor   # Filter by domain
    uv run python3 skills/home-assistant/scripts/ha.py state <entity_id>   # Single entity state
    uv run python3 skills/home-assistant/scripts/ha.py context             # Context snapshot
    uv run python3 skills/home-assistant/scripts/ha.py --json <subcommand> # JSON output

Credentials: HA_HOST and HA_API_KEY in a .env file anywhere in the CWD→root walk.
"""

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


class HAError(Exception):
    pass


def _find_env_file() -> Path | None:
    """Walk up from CWD looking for a .env file."""
    p = Path.cwd()
    while True:
        candidate = p / ".env"
        if candidate.exists():
            return candidate
        parent = p.parent
        if parent == p:
            return None
        p = parent


_env_path = _find_env_file()
if _env_path:
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip().strip('"').strip("'")
            os.environ.setdefault(_k.strip(), _v)

HA_HOST = os.environ.get("HA_HOST", "").rstrip("/")
HA_API_KEY = os.environ.get("HA_API_KEY", "")
TIMEOUT = 10


def _extract_hostname(url: str) -> str | None:
    """Extract hostname from a URL like https://ha.example.com."""
    if not url:
        return None
    no_proto = url.split("://")[-1] if "://" in url else url
    return no_proto.split(":")[0].split("/")[0]


def _resolve_hostname(hostname: str) -> str | None:
    """Resolve a hostname, returning IP or None."""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return None


def _diagnose_dns(hostname: str) -> None:
    """Print DNS diagnostics."""
    ip = _resolve_hostname(hostname)
    if ip:
        return  # DNS works
    print(f"  ✗ {hostname} does not resolve via system DNS", file=sys.stderr)
    try:
        hosts = Path("/etc/hosts").read_text()
        if hostname in hosts:
            line = next(ln for ln in hosts.splitlines() if hostname in ln)
            print(f"  ✓ Found in /etc/hosts: {line.strip()}", file=sys.stderr)
            return
    except OSError:
        pass
    print("  ✗ Not found in /etc/hosts either", file=sys.stderr)
    print("\n  Add the HA server IP to /etc/hosts:", file=sys.stderr)
    print(f"    echo '<IP> {hostname}' | sudo tee -a /etc/hosts", file=sys.stderr)


def ha_request(path: str, method: str = "GET", body: dict | None = None) -> Any:
    if not HA_HOST or not HA_API_KEY:
        print("ERROR: HA_HOST and HA_API_KEY must be set in .env", file=sys.stderr)
        sys.exit(1)
    url = f"{HA_HOST}/api/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body else None
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {HA_API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except URLError as e:
        hostname = _extract_hostname(HA_HOST)
        if hostname:
            _diagnose_dns(hostname)
        raise HAError(f"ERROR connecting to {url}: {e}") from e


def cmd_status(args: argparse.Namespace) -> None:
    data = ha_request("")
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"✓ Home Assistant {data.get('version', '?')} at {HA_HOST}")
        print(f"  Message: {data.get('message', '')}")


def cmd_states(args: argparse.Namespace) -> None:
    states: list[dict] = ha_request("states")
    if args.domain:
        states = [s for s in states if s["entity_id"].startswith(f"{args.domain}.")]
    if args.json:
        print(json.dumps(states, indent=2))
        return
    print(f"{'Entity':50} {'State':20} {'Last changed'}")
    print("-" * 90)
    for s in sorted(states, key=lambda x: x["entity_id"]):
        changed = s.get("last_changed", "")[:19]
        print(f"{s['entity_id']:50} {s['state']:20} {changed}")


def cmd_state(args: argparse.Namespace) -> None:
    s = ha_request(f"states/{args.entity_id}")
    if args.json:
        print(json.dumps(s, indent=2))
        return
    print(f"Entity:       {s['entity_id']}")
    print(f"State:        {s['state']}")
    print(f"Last changed: {s.get('last_changed', '')}")
    attrs = s.get("attributes", {})
    if attrs:
        print("Attributes:")
        for k, v in attrs.items():
            print(f"  {k}: {v}")


def cmd_location(args: argparse.Namespace) -> None:
    """Show person/device_tracker location states."""
    all_states: list[dict] = ha_request("states")
    persons = [s for s in all_states if s["entity_id"].startswith("person.")]
    trackers = [s for s in all_states if s["entity_id"].startswith("device_tracker.")]

    if args.json:
        print(json.dumps({"persons": persons, "device_trackers": trackers}, indent=2))
        return

    if persons:
        print("## Persons")
        for p in persons:
            name = p.get("attributes", {}).get("friendly_name", p["entity_id"])
            state = p["state"]
            lat = p.get("attributes", {}).get("latitude", "?")
            lon = p.get("attributes", {}).get("longitude", "?")
            gps = f" ({lat}, {lon})" if lat != "?" else ""
            print(f"  {name}: {state}{gps}")

    if trackers:
        print("\n## Device trackers")
        for t in trackers:
            name = t.get("attributes", {}).get("friendly_name", t["entity_id"])
            print(f"  {name}: {t['state']}")

    if not persons and not trackers:
        print("No person or device_tracker entities found.")


def cmd_persons(args: argparse.Namespace) -> None:
    args.domain = "person"
    cmd_states(args)


def cmd_calendar(args: argparse.Namespace) -> None:
    """Show upcoming calendar events (next 7 days)."""
    all_states: list[dict] = ha_request("states")
    cal_entities = [
        s["entity_id"] for s in all_states if s["entity_id"].startswith("calendar.")
    ]

    if not cal_entities:
        print("No calendar entities found.")
        return

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    # HA REST API requires Z suffix for UTC, not +00:00 offset format
    start_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events: list[dict] = []
    for cal_id in cal_entities:
        try:
            events = ha_request(f"calendars/{cal_id}?start={start_str}&end={end_str}")
            for e in events:
                e["_calendar"] = cal_id
            all_events.extend(events)
        except HAError:
            pass

    all_events.sort(
        key=lambda e: e.get("start", {}).get(
            "dateTime", e.get("start", {}).get("date", "")
        )
    )

    if args.json:
        print(json.dumps(all_events, indent=2))
        return

    if not all_events:
        print(f"No events in the next 7 days across: {', '.join(cal_entities)}")
        return

    print(f"## Upcoming events (next 7 days) from {len(cal_entities)} calendar(s)\n")
    for e in all_events:
        start = e.get("start", {})
        dt = start.get("dateTime", start.get("date", "?"))[:16]
        summary = e.get("summary", "(no title)")
        cal = e.get("_calendar", "")
        print(f"  {dt}  {summary}  [{cal}]")


def cmd_context(args: argparse.Namespace) -> None:
    """Context snapshot: persons, weather, next calendar event, recent automations."""
    states: list[dict] = ha_request("states")

    def attr(s: dict, key: str, default: str = "") -> str:
        return str(s.get("attributes", {}).get(key, default))

    # Discover persons dynamically
    persons = [s for s in states if s["entity_id"].startswith("person.")]

    # First available weather entity
    weather_entities = [s for s in states if s["entity_id"].startswith("weather.")]
    weather_state = weather_entities[0]["state"] if weather_entities else "?"
    weather_temp = attr(weather_entities[0], "temperature") if weather_entities else ""

    # Next calendar event (next 4h, first calendar entity found)
    next_event = None
    cal_entities = [
        s["entity_id"] for s in states if s["entity_id"].startswith("calendar.")
    ]
    if cal_entities:
        try:
            now = datetime.now(timezone.utc)
            end = now + timedelta(hours=4)
            events = ha_request(
                f"calendars/{cal_entities[0]}"
                f"?start={now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )
            if events:
                e = events[0]
                start = e.get("start", {})
                dt = start.get("dateTime", start.get("date", "?"))[:16]
                next_event = f"{dt} {e.get('summary', '')}"
        except HAError:
            pass

    # Recently triggered automations (last 24h)
    notable_automations = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for s in states:
        if not s["entity_id"].startswith("automation."):
            continue
        last = s.get("attributes", {}).get("last_triggered")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if last_dt > cutoff:
            notable_automations.append(
                {
                    "name": s.get("attributes", {}).get(
                        "friendly_name", s["entity_id"]
                    ),
                    "last_triggered": last,
                }
            )
    notable_automations.sort(key=lambda x: x["last_triggered"], reverse=True)

    persons_data = {
        p["entity_id"]: {
            "location": p["state"],
            "friendly_name": p.get("attributes", {}).get(
                "friendly_name", p["entity_id"]
            ),
        }
        for p in persons
    }

    if args.json:
        print(
            json.dumps(
                {
                    "persons": persons_data,
                    "weather": {"state": weather_state, "temp_c": weather_temp},
                    "next_event_4h": next_event,
                    "automations_24h": notable_automations[:10],
                },
                indent=2,
            )
        )
        return

    print(f"## HA context @ {datetime.now(timezone.utc).strftime('%H:%MZ')}")
    for p in persons:
        name = p.get("attributes", {}).get("friendly_name", p["entity_id"])
        print(f"- {name}: {p['state']}")
    if weather_entities:
        print(f"- Weather: {weather_state}, {weather_temp}°C")
    print(f"- Next 4h: {next_event or '(nothing scheduled)'}")
    if notable_automations:
        print(f"\n## Automations triggered in last 24h ({len(notable_automations)})")
        for a in notable_automations[:5]:
            print(f"  {a['last_triggered'][:19]}  {a['name']}")


def cmd_cameras(args: argparse.Namespace) -> None:
    all_states: list[dict] = ha_request("states")
    cameras = [s for s in all_states if s["entity_id"].startswith("camera.")]

    if args.json:
        print(json.dumps(cameras, indent=2))
        return

    if not cameras:
        print("No camera entities found.")
        return

    print(f"Found {len(cameras)} camera(s):")
    for c in cameras:
        name = c.get("attributes", {}).get("friendly_name", c["entity_id"])
        print(f"  {c['entity_id']}: {name} (state: {c['state']})")

    if args.snapshot:
        outdir = Path(args.snapshot)
        outdir.mkdir(parents=True, exist_ok=True)
        print(f"\nSaving snapshots to {outdir}/")
        for c in cameras:
            eid = c["entity_id"]
            url = f"{HA_HOST}/api/camera_proxy/{eid}"
            req = Request(url, headers={"Authorization": f"Bearer {HA_API_KEY}"})
            try:
                with urlopen(req, timeout=TIMEOUT) as resp:
                    img_data = resp.read()
                out = outdir / f"{eid.replace('.', '_')}.jpg"
                out.write_bytes(img_data)
                print(f"  Saved {out} ({len(img_data) // 1024}KB)")
            except Exception as e:
                print(f"  Failed {eid}: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Test HA connectivity")
    p_states = sub.add_parser("states", help="List entity states")
    p_states.add_argument(
        "--domain", help="Filter by domain (e.g. sensor, device_tracker)"
    )
    p_state = sub.add_parser("state", help="Get single entity state")
    p_state.add_argument("entity_id", help="Entity ID (e.g. person.alice)")
    sub.add_parser("location", help="Show person/device_tracker states")
    sub.add_parser("persons", help="Show all person entity states")
    sub.add_parser("calendar", help="Show upcoming calendar events (7 days)")
    sub.add_parser(
        "context",
        help="Context snapshot: persons, weather, next event, recent automations",
    )
    p_cameras = sub.add_parser(
        "cameras", help="List cameras and optionally save snapshots"
    )
    p_cameras.add_argument(
        "--snapshot", metavar="DIR", help="Save JPEG snapshots to DIR"
    )

    args = parser.parse_args()
    cmds = {
        "status": cmd_status,
        "states": cmd_states,
        "state": cmd_state,
        "location": cmd_location,
        "persons": cmd_persons,
        "calendar": cmd_calendar,
        "context": cmd_context,
        "cameras": cmd_cameras,
    }
    try:
        cmds[args.cmd](args)
    except HAError as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
