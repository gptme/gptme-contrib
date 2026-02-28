"""Fetch ActivityWatch data for summarization context.

Uses the aw-client library to query window activity and time tracking data
from a local ActivityWatch server.

Default AW server: http://localhost:5600 (configurable via AW_SERVER env var)
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

logger = logging.getLogger(__name__)

AW_SERVER = os.environ.get("AW_SERVER", "http://localhost:5600")
AW_TIMEOUT = 5  # seconds


@dataclass
class AppUsage:
    """Time spent in a single application."""

    app: str
    duration: float  # seconds
    title_count: int = 0  # number of distinct window titles seen


@dataclass
class BrowserDomain:
    """Time spent on a web domain."""

    domain: str
    duration: float  # seconds


@dataclass
class AWActivity:
    """Aggregated ActivityWatch data for a time period."""

    start_date: date
    end_date: date
    total_active_seconds: float = 0.0
    top_apps: list[AppUsage] = field(default_factory=list)
    top_domains: list[BrowserDomain] = field(default_factory=list)
    # AW was reachable and returned data
    available: bool = False

    @property
    def total_active_hours(self) -> float:
        return self.total_active_seconds / 3600


def _get_client() -> Any:
    """Create an aw-client instance.

    Returns the client or None if aw-client is not available.
    """
    try:
        from aw_client import ActivityWatchClient
    except ImportError:
        logger.debug("aw-client not installed")
        return None

    # Parse host and port from AW_SERVER
    host = "localhost"
    port = 5600
    server = AW_SERVER.replace("http://", "").replace("https://", "")
    if ":" in server:
        parts = server.split(":")
        host = parts[0]
        try:
            port = int(parts[1].rstrip("/"))
        except ValueError:
            pass
    elif server:
        host = server.rstrip("/")

    return ActivityWatchClient(
        "gptme-activity-summary",
        host=host,
        port=port,
        testing=False,
    )


def _find_bucket(client: Any, prefix: str) -> str | None:
    """Find a bucket by prefix (e.g. 'aw-watcher-window_')."""
    try:
        buckets = client.get_buckets()
    except Exception:
        return None
    for bucket_id in buckets:
        if str(bucket_id).startswith(prefix):
            return str(bucket_id)
    return None


def _build_timeperiod(start: date, end: date) -> tuple[datetime, datetime]:
    """Build a (start_dt, end_dt) tuple for aw-client query.

    Args:
        start: Start date (inclusive)
        end: End date (inclusive — will be extended to next day)
    """
    start_dt = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=timezone.utc)
    end_next = end + timedelta(days=1)
    end_dt = datetime(end_next.year, end_next.month, end_next.day, 0, 0, 0, tzinfo=timezone.utc)
    return (start_dt, end_dt)


def _run_aw_query(
    client: Any, query: list[str], timeperiod: tuple[datetime, datetime]
) -> list | None:
    """Run an AW query via aw-client and return the result list.

    Args:
        client: ActivityWatchClient instance
        query: List of AW query language statements
        timeperiod: (start, end) datetime tuple

    Returns:
        Query result list, or None on failure
    """
    try:
        query_str = "\n".join(query)
        result = client.query(query_str, [timeperiod])
        if isinstance(result, list) and result:
            return cast(list, result[0])  # First (only) timeperiod result
    except Exception as e:
        logger.debug("AW query failed: %s", e)
    return None


def _fetch_app_usage(
    client: Any, window_bucket: str, afk_bucket: str | None, timeperiod: tuple[datetime, datetime]
) -> tuple[list[AppUsage], float]:
    """Fetch per-app time usage, filtering out AFK periods when possible.

    When an AFK bucket is available, window events are intersected with
    not-afk periods to produce accurate active-only durations.

    Returns:
        Tuple of (app_usage_list, total_active_seconds)
    """
    if afk_bucket:
        # Filter window events to only include time when user was not AFK.
        # This is the standard AW pattern used by aw-webui.
        query = [
            f'afk_events = query_bucket("{afk_bucket}");',
            'afk_events = filter_keyvals(afk_events, "status", ["not-afk"]);',
            f'window_events = query_bucket("{window_bucket}");',
            "events = filter_period_intersect(window_events, afk_events);",
            'events = merge_events_by_keys(events, ["app"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]
    else:
        # No AFK bucket — fall back to raw window events
        query = [
            f'events = query_bucket("{window_bucket}");',
            'events = merge_events_by_keys(events, ["app"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]

    results = _run_aw_query(client, query, timeperiod)
    if results is None:
        return [], 0.0

    top_apps: list[AppUsage] = []
    total_seconds = 0.0

    for event in results:
        if not isinstance(event, dict):
            continue
        duration = float(event.get("duration", 0))
        data = event.get("data", {})
        app = data.get("app", "Unknown")
        if duration < 1:
            continue
        top_apps.append(AppUsage(app=app, duration=duration))
        total_seconds += duration

    return top_apps, total_seconds


def _fetch_browser_domains(
    client: Any, web_bucket: str, afk_bucket: str | None, timeperiod: tuple[datetime, datetime]
) -> list[BrowserDomain]:
    """Fetch top domains from aw-watcher-web, with optional AFK filtering."""
    if afk_bucket:
        query = [
            f'afk_events = query_bucket("{afk_bucket}");',
            'afk_events = filter_keyvals(afk_events, "status", ["not-afk"]);',
            f'web_events = query_bucket("{web_bucket}");',
            "events = filter_period_intersect(web_events, afk_events);",
            'events = merge_events_by_keys(events, ["$domain"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]
    else:
        query = [
            f'web_events = query_bucket("{web_bucket}");',
            'events = merge_events_by_keys(web_events, ["$domain"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]

    results = _run_aw_query(client, query, timeperiod)
    if results is None:
        return []

    domains: list[BrowserDomain] = []
    for event in results:
        if not isinstance(event, dict):
            continue
        duration = float(event.get("duration", 0))
        data = event.get("data", {})
        domain = data.get("$domain", "")
        if duration < 1 or not domain:
            continue
        domains.append(BrowserDomain(domain=domain, duration=duration))

    return domains


def fetch_aw_activity(start: date, end: date) -> AWActivity:
    """Fetch ActivityWatch activity data for a date range.

    Queries window watcher data and aggregates time per app.
    Filters out AFK periods when the AFK watcher is available.
    Optionally includes browser domain data from aw-watcher-web.
    Handles the case where AW is not running (returns empty activity).

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)

    Returns:
        AWActivity with aggregated app usage data.
    """
    activity = AWActivity(start_date=start, end_date=end)

    client = _get_client()
    if client is None:
        logger.debug("aw-client not available")
        return activity

    try:
        client.get_info()
    except Exception:
        logger.debug("ActivityWatch server not available at %s", AW_SERVER)
        return activity

    window_bucket = _find_bucket(client, "aw-watcher-window_")
    if not window_bucket:
        logger.debug("No aw-watcher-window bucket found")
        activity.available = True  # AW is running, just no window bucket
        return activity

    afk_bucket = _find_bucket(client, "aw-watcher-afk_")
    web_bucket = _find_bucket(client, "aw-watcher-web-")

    timeperiod = _build_timeperiod(start, end)

    # Fetch app usage (with AFK filtering when available)
    top_apps, total_seconds = _fetch_app_usage(client, window_bucket, afk_bucket, timeperiod)
    if not top_apps:
        activity.available = True
        return activity

    activity.available = True
    activity.top_apps = top_apps
    activity.total_active_seconds = total_seconds

    # Fetch browser domains (optional — only if web watcher is present)
    if web_bucket:
        activity.top_domains = _fetch_browser_domains(client, web_bucket, afk_bucket, timeperiod)

    return activity


def format_aw_activity_for_prompt(activity: AWActivity) -> str:
    """Format ActivityWatch activity as markdown for LLM prompts.

    Returns empty string if AW was not available or no data.
    """
    if not activity.available or not activity.top_apps:
        return ""

    lines: list[str] = []
    lines.append("## ActivityWatch — Time Tracking")
    lines.append(f"Period: {activity.start_date.isoformat()} to {activity.end_date.isoformat()}")
    total_h = activity.total_active_hours
    lines.append(f"- **Total active time**: {total_h:.1f}h ({activity.total_active_seconds:.0f}s)")
    lines.append("")

    lines.append("### Top Applications")
    for app in activity.top_apps[:15]:  # Top 15 apps
        pct = (
            (app.duration / activity.total_active_seconds * 100)
            if activity.total_active_seconds > 0
            else 0
        )
        h = app.duration / 3600
        lines.append(f"- **{app.app}**: {h:.1f}h ({pct:.0f}%)")

    if activity.top_domains:
        lines.append("")
        lines.append("### Top Websites")
        for domain in activity.top_domains[:10]:  # Top 10 domains
            h = domain.duration / 3600
            if h >= 0.1:
                lines.append(f"- **{domain.domain}**: {h:.1f}h")
            else:
                m = domain.duration / 60
                lines.append(f"- **{domain.domain}**: {m:.0f}min")

    lines.append("")
    return "\n".join(lines)
