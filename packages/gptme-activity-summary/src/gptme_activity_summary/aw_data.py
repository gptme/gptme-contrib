"""Fetch ActivityWatch data for summarization context.

Uses the aw-client library to query window activity and time tracking data
from a local ActivityWatch server.

Default AW server: http://localhost:5600 (configurable via AW_SERVER env var)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from urllib.request import urlopen

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
class CategoryUsage:
    """Time spent in an AW category (from user's categorization rules)."""

    category: list[str]  # Category path, e.g. ["Coding", "Python"] or ["Uncategorized"]
    duration: float  # seconds

    @property
    def name(self) -> str:
        """Human-readable category path joined with ' > '."""
        return " > ".join(self.category) if self.category else "Uncategorized"

    @property
    def top_level(self) -> str:
        """Top-level category name (first element of path)."""
        return self.category[0] if self.category else "Uncategorized"


@dataclass
class AWActivity:
    """Aggregated ActivityWatch data for a time period."""

    start_date: date
    end_date: date
    total_active_seconds: float = 0.0
    top_apps: list[AppUsage] = field(default_factory=list)
    top_domains: list[BrowserDomain] = field(default_factory=list)
    categories: list[CategoryUsage] = field(default_factory=list)
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


def _fetch_category_rules() -> list[list[Any]]:
    """Fetch category rules from the ActivityWatch settings API.

    AW webui persists categorization rules under the ``classes`` setting and
    injects them directly into queries. The Python aw-client does not expose a
    settings helper, so we read the REST endpoint directly and convert the saved
    categories into the ``[category_path, rule]`` shape expected by
    ``categorize(...)``.

    Returns an empty list when no classes are saved or the settings endpoint is
    unavailable. That fallback still produces a valid ``categorize(events, [])``
    query, which yields ``["Uncategorized"]`` instead of a server-side parse
    error.
    """
    settings_url = f"{AW_SERVER.rstrip('/')}/api/0/settings/classes"
    try:
        with urlopen(settings_url, timeout=AW_TIMEOUT) as response:
            payload = json.load(response)
    except Exception as e:
        logger.debug("AW category settings fetch failed: %s", e)
        return []

    if not isinstance(payload, list):
        return []

    rules: list[list[Any]] = []
    for category in payload:
        if not isinstance(category, dict):
            continue
        name = category.get("name")
        rule = category.get("rule")
        if not isinstance(name, list) or not isinstance(rule, dict):
            continue
        if rule.get("type") is None:
            continue
        rules.append([name, rule])
    return rules


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


def _fetch_category_usage(
    client: Any, window_bucket: str, afk_bucket: str | None, timeperiod: tuple[datetime, datetime]
) -> list[CategoryUsage]:
    """Fetch time per category using AW's saved categorization rules.

    Uses AW's built-in ``categorize()`` function with rules loaded from the
    ActivityWatch settings API. Returns an empty list only if the query fails.

    When no categories are configured, AW assigns all events to ["Uncategorized"],
    so this returns [CategoryUsage(["Uncategorized"], total_duration)] rather than [].
    Use the meaningful_categories filter (top_level != "Uncategorized") to detect this case.

    The $category field in results is a list like ["Coding", "Python"] or ["Uncategorized"].
    """
    category_rules = json.dumps(_fetch_category_rules())
    if afk_bucket:
        query = [
            f'afk_events = query_bucket("{afk_bucket}");',
            'afk_events = filter_keyvals(afk_events, "status", ["not-afk"]);',
            f'window_events = query_bucket("{window_bucket}");',
            "events = filter_period_intersect(window_events, afk_events);",
            f"events = categorize(events, {category_rules});",
            'events = merge_events_by_keys(events, ["$category"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]
    else:
        query = [
            f'window_events = query_bucket("{window_bucket}");',
            f"events = categorize(window_events, {category_rules});",
            'events = merge_events_by_keys(events, ["$category"]);',
            "events = sort_by_duration(events);",
            "RETURN = events;",
        ]

    results = _run_aw_query(client, query, timeperiod)
    if results is None:
        return []

    categories: list[CategoryUsage] = []
    for event in results:
        if not isinstance(event, dict):
            continue
        duration = float(event.get("duration", 0))
        data = event.get("data", {})
        category = data.get("$category", [])
        if not isinstance(category, list):
            category = [str(category)]
        if duration < 1:
            continue
        categories.append(CategoryUsage(category=category, duration=duration))

    return categories


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

    # Fetch category breakdown (always fetched; conditionality is in the display layer —
    # when the user has no rules, AW returns all events as ["Uncategorized"] which is
    # suppressed by the meaningful_categories filter in format_aw_activity_for_prompt)
    activity.categories = _fetch_category_usage(client, window_bucket, afk_bucket, timeperiod)

    return activity


def format_aw_activity_for_prompt(activity: AWActivity) -> str:
    """Format ActivityWatch activity as markdown for LLM prompts.

    Returns empty string if AW was not available or no data.

    When categories are available (user has configured AW categorization rules),
    the category breakdown is shown first as a high-level summary, followed by
    the per-app breakdown for detail. Category percentages are relative to
    total categorized time (sum of all category durations), not total_active_seconds.
    """
    if not activity.available or not activity.top_apps:
        return ""

    lines: list[str] = []
    lines.append("## ActivityWatch — Time Tracking")
    lines.append(f"Period: {activity.start_date.isoformat()} to {activity.end_date.isoformat()}")
    total_h = activity.total_active_hours
    lines.append(f"- **Total active time**: {total_h:.1f}h ({activity.total_active_seconds:.0f}s)")
    lines.append("")

    # Show category breakdown only when user has actual category rules configured.
    # When no rules exist, AW returns all events as ["Uncategorized"], which would
    # produce a meaningless "Uncategorized: 100%" section — suppress that case.
    # Note: meaningful_categories is used only as a gate (has the user configured rules?).
    # The display loop below uses activity.categories (all categories, including Uncategorized)
    # so that when real rules are configured, the "Uncategorized" remainder is still shown.
    meaningful_categories = [c for c in activity.categories if c.top_level != "Uncategorized"]
    if meaningful_categories:
        lines.append("### Time by Category")
        # Use category total as denominator — avoids dependency on the separate
        # _fetch_app_usage() total which comes from a different HTTP round-trip.
        category_total = sum(c.duration for c in activity.categories)
        max_cats = 12
        sorted_cats = sorted(activity.categories, key=lambda c: c.duration, reverse=True)
        shown = sorted_cats[:max_cats]
        for cat in shown:
            pct = (cat.duration / category_total * 100) if category_total > 0 else 0
            h = cat.duration / 3600
            lines.append(f"- **{cat.name}**: {h:.1f}h ({pct:.0f}%)")
        if len(sorted_cats) > max_cats:
            omitted = len(sorted_cats) - max_cats
            omitted_h = sum(c.duration for c in sorted_cats[max_cats:]) / 3600
            lines.append(f"- *...{omitted} more categories ({omitted_h:.1f}h not shown)*")
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
