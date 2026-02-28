"""Fetch ActivityWatch data for summarization context.

Uses the AW REST API directly (no aw-client dependency required) to query
window activity and time tracking data from a local ActivityWatch server.

Default AW server: http://localhost:5600 (configurable via AW_SERVER env var)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen, Request

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
class AWActivity:
    """Aggregated ActivityWatch data for a time period."""

    start_date: date
    end_date: date
    total_active_seconds: float = 0.0
    top_apps: list[AppUsage] = field(default_factory=list)
    # AW was reachable and returned data
    available: bool = False

    @property
    def total_active_hours(self) -> float:
        return self.total_active_seconds / 3600


def _aw_request(path: str, method: str = "GET", body: dict | None = None) -> dict | list | None:
    """Make a request to the AW REST API.

    Returns parsed JSON or None on failure.
    """
    url = f"{AW_SERVER}{path}"
    try:
        headers = {"Content-Type": "application/json"}
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=AW_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        logger.debug("AW server not reachable: %s", e)
        return None
    except Exception as e:
        logger.debug("AW request failed (%s %s): %s", method, path, e)
        return None


def _aw_available() -> bool:
    """Check if the ActivityWatch server is running."""
    result = _aw_request("/api/0/info")
    return result is not None


def _get_window_bucket() -> str | None:
    """Find the aw-watcher-window bucket name for this host."""
    buckets = _aw_request("/api/0/buckets/")
    if not isinstance(buckets, dict):
        return None
    for bucket_id in buckets:
        if bucket_id.startswith("aw-watcher-window_"):
            return bucket_id
    return None


def _run_aw_query(query: list[str], timeperiod: str) -> list | None:
    """Run an AW query and return the result list.

    Args:
        query: List of AW query language statements
        timeperiod: ISO 8601 interval string, e.g. "2024-01-01T00:00:00/2024-01-02T00:00:00"

    Returns:
        Query result list, or None on failure
    """
    body = {
        "query": query,
        "timeperiods": [timeperiod],
    }
    result = _aw_request("/api/0/query/", method="POST", body=body)
    if isinstance(result, list) and result:
        return result[0]  # First (only) timeperiod result
    return None


def fetch_aw_activity(start: date, end: date) -> AWActivity:
    """Fetch ActivityWatch activity data for a date range.

    Queries window watcher data and aggregates time per app.
    Handles the case where AW is not running (returns empty activity).

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)

    Returns:
        AWActivity with aggregated app usage data.
    """
    activity = AWActivity(start_date=start, end_date=end)

    if not _aw_available():
        logger.debug("ActivityWatch server not available at %s", AW_SERVER)
        return activity

    window_bucket = _get_window_bucket()
    if not window_bucket:
        logger.debug("No aw-watcher-window bucket found")
        activity.available = True  # AW is running, just no window bucket
        return activity

    # Build time period string
    # AW timeperiods are ISO 8601 intervals: start/end (exclusive end)
    start_dt = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=timezone.utc)
    # Add one day to end to make it inclusive
    from datetime import timedelta

    end_next = end + timedelta(days=1)
    end_dt = datetime(end_next.year, end_next.month, end_next.day, 0, 0, 0, tzinfo=timezone.utc)
    timeperiod = f"{start_dt.isoformat()}/{end_dt.isoformat()}"

    # Query: get non-AFK window events, merge by app, sort by duration
    # This uses AW's query language to aggregate on the server side
    query = [
        f'events = query_bucket("{window_bucket}")',
        'events = merge_events_by_keys(events, ["app"])',
        "events = sort_by_duration(events)",
        "RETURN = events",
    ]

    results = _run_aw_query(query, timeperiod)
    if results is None:
        activity.available = True  # AW running but query failed
        return activity

    activity.available = True
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

    activity.top_apps = top_apps
    activity.total_active_seconds = total_seconds

    return activity


def format_aw_activity_for_prompt(activity: AWActivity) -> str:
    """Format ActivityWatch activity as markdown for LLM prompts.

    Returns empty string if AW was not available or no data.
    """
    if not activity.available or not activity.top_apps:
        return ""

    lines: list[str] = []
    lines.append("## ActivityWatch — Time Tracking (Real Data)")
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

    lines.append("")
    return "\n".join(lines)
