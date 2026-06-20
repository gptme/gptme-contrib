#!/usr/bin/env python3
"""Parse CC /usage TUI output from stdin and emit JSON or human-readable format.

Called by check-claude-usage.sh with --json or --human flags.
Reads raw tmux-captured TUI output from stdin and extracts utilization data.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone


def parse_reset_time(reset_str: str) -> datetime | None:
    """Parse CC's reset time string into a datetime."""
    now = datetime.now(timezone.utc)
    try:
        s = reset_str.replace("(UTC)", "").strip()

        # Format: '9pm' or '9:30pm' (today or tomorrow)
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", s, re.IGNORECASE)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = m.group(3).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset_dt <= now:
                reset_dt += timedelta(days=1)
            return reset_dt

        # Format: 'Jun 23, 4pm' or 'Jun 23, 7:59am'
        m = re.match(
            r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$",
            s,
            re.IGNORECASE,
        )
        if m:
            months = {
                "jan": 1,
                "feb": 2,
                "mar": 3,
                "apr": 4,
                "may": 5,
                "jun": 6,
                "jul": 7,
                "aug": 8,
                "sep": 9,
                "oct": 10,
                "nov": 11,
                "dec": 12,
            }
            month = months.get(m.group(1).lower()[:3], now.month)
            day = int(m.group(2))
            hour = int(m.group(3))
            minute = int(m.group(4) or 0)
            ampm = m.group(5).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            reset_dt = datetime(now.year, month, day, hour, minute, tzinfo=timezone.utc)
            if reset_dt < now:
                reset_dt = reset_dt.replace(year=now.year + 1)
            return reset_dt

    except Exception:
        pass
    return None


def format_time_left(reset_dt: datetime) -> str:
    """Format a human-readable time-left string."""
    if not reset_dt:
        return ""
    delta = reset_dt - datetime.now(timezone.utc)
    total_sec = delta.total_seconds()
    if total_sec <= 0:
        return "(resetting now)"
    hours = total_sec / 3600
    if hours >= 48:
        return f"{hours / 24:.1f}d left"
    elif hours >= 1:
        return f"{int(hours)}h{int((hours - int(hours)) * 60):02d}m left"
    else:
        return f"{int(total_sec / 60)}m left"


def parse_section_headers(lines: list[str]) -> dict:
    """Parse CC v2.1.183+ section-header format.

    Looks for:
      Current session
      █  N% used
      Resets ...

      Current week (all models)
         N% used
      Resets ...

    Returns dict with five_hour, seven_day, seven_day_sonnet keys, or empty.
    """
    section_headers = [
        ("Current session", "five_hour"),
        ("Current week (all models)", "seven_day"),
        ("Current week (Sonnet only)", "seven_day_sonnet"),
    ]
    result = {}
    for header, key in section_headers:
        for i, line in enumerate(lines):
            if header in line:
                for j in range(i + 1, min(i + 10, len(lines))):
                    pct_m = re.search(r"(\d+)%\s*used", lines[j])
                    if pct_m:
                        pct = int(pct_m.group(1)) / 100.0
                        reset_str = None
                        for k in range(j + 1, min(j + 5, len(lines))):
                            reset_m = re.search(r"Resets\s+(.+)", lines[k])
                            if reset_m:
                                reset_str = reset_m.group(1).strip()
                                break
                        result[key] = {
                            "utilization": pct,
                            "resets": reset_str if reset_str else "unknown",
                        }
                        break
    return result if len(result) >= 2 else {}


def build_low_usage_result() -> dict:
    """Build a zero-utilization result for the 'Nothing over 10%' case."""
    now = datetime.now(timezone.utc)
    days_until_thu = (3 - now.weekday()) % 7
    if days_until_thu == 0:
        days_until_thu = 7
    weekly_reset = now + timedelta(days=days_until_thu)
    weekly_reset = weekly_reset.replace(hour=0, minute=0, second=0, microsecond=0)
    weekly_secs = max(0, int((weekly_reset - now).total_seconds()))
    weekly_reset_str = weekly_reset.strftime("%a, %-I%p")
    five_hour_reset = now + timedelta(hours=5)
    five_hour_secs = int((five_hour_reset - now).total_seconds())
    five_hour_reset_str = five_hour_reset.strftime("%-I:%M%p").lower()

    return {
        "five_hour": {
            "utilization": 0.0,
            "resets": five_hour_reset_str,
            "resets_in_seconds": five_hour_secs,
            "time_left": format_time_left(five_hour_reset),
        },
        "seven_day": {
            "utilization": 0.0,
            "resets": weekly_reset_str,
            "resets_in_seconds": weekly_secs,
            "time_left": format_time_left(weekly_reset),
        },
        "seven_day_sonnet": {
            "utilization": 0.0,
            "resets": weekly_reset_str,
            "resets_in_seconds": weekly_secs,
            "time_left": format_time_left(weekly_reset),
        },
    }


def main():
    text = sys.stdin.read()
    lines = text.split("\n")
    json_mode = "--json" in sys.argv

    # Cache args: --cache-file PATH --cred-fingerprint FP
    cache_file = None
    cred_fingerprint = ""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--cache-file" and i + 1 < len(argv):
            cache_file = argv[i + 1]
        elif arg == "--cred-fingerprint" and i + 1 < len(argv):
            cred_fingerprint = argv[i + 1]

    # Error checks
    if "Error:" in text and "scope" in text.lower():
        print(
            "Error: OAuth token missing required scope. Re-login with /login in CC.",
            file=sys.stderr,
        )
        sys.exit(1)

    if "Claude API" in text and "Max" not in text:
        print(
            "Warning: Running in API-key mode (not Max subscription). No quota data.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Try section-header parser first (CC v2.1.183+)
    result = parse_section_headers(lines)

    # Fallback: low usage / empty result
    if not result:
        if "Nothing over 10%" in text:
            result = build_low_usage_result()
        else:
            print(
                "Error: Could not parse usage data.\n"
                "This script requires Claude Code v2.1.183 or later.\n"
                "If you're on an older version, upgrade with: claude update",
                file=sys.stderr,
            )
            print("Run with --raw to see raw output for debugging.", file=sys.stderr)
            sys.exit(1)

    # Add time_left and resets_in_seconds to all entries
    for info in result.values():
        if not isinstance(info, dict):
            continue
        reset_str = info.get("resets", "")
        if reset_str and reset_str not in ("unknown", "session_end"):
            reset_dt = parse_reset_time(reset_str)
            if reset_dt:
                delta = reset_dt - datetime.now(timezone.utc)
                info["resets_in_seconds"] = max(0, int(delta.total_seconds()))
                info["time_left"] = format_time_left(reset_dt)
        elif "resets_in_seconds" not in info:
            info["resets_in_seconds"] = 0
            info["time_left"] = ""

    # Write cache if requested (skip on error paths — those exit before reaching here)
    if cache_file:
        cache_result = dict(result)
        cache_result["_cred_fingerprint"] = cred_fingerprint
        try:
            with open(cache_file, "w") as f:
                json.dump(cache_result, f, indent=2)
        except OSError:
            pass

    # Output
    if json_mode:
        print(json.dumps(result, indent=2))
    else:
        print("Claude Max Subscription Usage")
        print("=" * 60)
        for key, label in [
            ("five_hour", "Session (5h)"),
            ("seven_day", "Weekly (all)"),
            ("seven_day_sonnet", "Weekly (Sonnet)"),
        ]:
            info = result.get(key)
            if info and isinstance(info, dict):
                util = info["utilization"]
                remaining = 1 - util
                bar_width = 30
                filled = int(util * bar_width)
                bar = "█" * filled + "░" * (bar_width - filled)
                time_left = info.get("time_left", "")
                resets = info.get("resets", "unknown")
                print(
                    f"  {label:20s} [{bar}] {util*100:4.0f}% used ({remaining*100:.0f}% left)"
                )
                print(f"  {'':20s} resets {resets}  ({time_left})")
            else:
                print(f"  {label:20s} N/A")
        print()


if __name__ == "__main__":
    main()
