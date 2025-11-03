#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "feedparser>=6.0.0",
#   "click>=8.0.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "pyyaml>=6.0.0",
#   "requests>=2.31.0",
#   "beautifulsoup4>=4.12.0",
# ]
# [tool.uv]
# exclude-newer = "2024-01-23T00:00:00Z"
# ///
"""
RSS reader tool for gptme-bob.

Reads RSS feeds and displays them in a compact format.
"""

import hashlib
import html
import json as json_lib
import os
import pickle
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import click
import feedparser
import requests
import yaml
from bs4 import BeautifulSoup  # type: ignore
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# Load environment variables from .env file
load_dotenv()

console = Console()


@dataclass
class FeedValidationResult:
    """Results from feed validation."""

    url: str
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    def add_error(self, message: str) -> None:
        """Add an error and mark feed as invalid."""
        self.is_valid = False
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        """Add a warning (doesn't invalidate feed)."""
        self.warnings.append(message)


@dataclass
class SearchHistoryEntry:
    """Entry in search history."""

    timestamp: str
    query: str
    field: str
    results_count: int


def load_search_history(
    cache_dir: Path | str = "~/.cache/rss_reader",
) -> list[SearchHistoryEntry]:
    """Load search history from file."""
    cache_path = Path(cache_dir).expanduser() / "search_history.json"
    if not cache_path.exists():
        return []

    try:
        with open(cache_path, "r") as f:
            data = json_lib.load(f)
        return [SearchHistoryEntry(**entry) for entry in data]
    except (json_lib.JSONDecodeError, TypeError):
        return []


def save_to_search_history(
    query: str,
    field: str,
    results_count: int,
    cache_dir: Path | str = "~/.cache/rss_reader",
    max_entries: int = 50,
) -> None:
    """Save search to history file."""
    cache_path = Path(cache_dir).expanduser()
    cache_path.mkdir(parents=True, exist_ok=True)
    history_path = cache_path / "search_history.json"

    # Load existing history
    history = load_search_history(cache_dir)

    # Add new entry
    new_entry = SearchHistoryEntry(
        timestamp=datetime.now().isoformat(),
        query=query,
        field=field,
        results_count=results_count,
    )

    history.append(new_entry)

    # Keep only last max_entries
    history = history[-max_entries:]

    # Save to file
    with open(history_path, "w") as f:
        json_lib.dump(
            [
                {
                    "timestamp": e.timestamp,
                    "query": e.query,
                    "field": e.field,
                    "results_count": e.results_count,
                }
                for e in history
            ],
            f,
            indent=2,
        )


def format_search_history(history: list[SearchHistoryEntry]) -> str:
    """Format search history for display."""
    if not history:
        return "No search history found."

    output = ["Search History (most recent first):\n"]
    for entry in reversed(history[-20:]):  # Show last 20
        dt = datetime.fromisoformat(entry.timestamp)
        output.append(
            f"  {dt.strftime('%Y-%m-%d %H:%M')} - '{entry.query}' in {entry.field} ({entry.results_count} results)"
        )

    return "\n".join(output)


def search_entries(
    entries: list[tuple[datetime, str, Any]],
    query: str,
    field: str = "all",
) -> list[tuple[datetime, str, Any]]:
    """Search entries for query in specified field.

    Args:
        entries: List of (datetime, source, entry) tuples
        query: Search query (regex pattern)
        field: Field to search in ('title', 'summary', 'link', 'all')

    Returns:
        Filtered list of entries matching the search query
    """
    if not query:
        return entries

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as e:
        console.print(f"[red]Invalid regex pattern: {e}[/red]")
        return entries

    filtered = []
    for dt, source, entry in entries:
        match = False

        if field in ("title", "all"):
            if pattern.search(entry.title):
                match = True

        if field in ("summary", "all"):
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            if summary and pattern.search(summary):
                match = True

        if field in ("link", "all"):
            if pattern.search(entry.link):
                match = True

        if match:
            filtered.append((dt, source, entry))

    return filtered


def parse_date(entry: Any) -> datetime:
    """Parse date from feed entry.

    Args:
        entry: Feed entry with published_parsed or updated_parsed

    Returns:
        Datetime object or current time if no date found
    """
    dt = entry.get("updated_parsed") or entry.get("published_parsed")
    if dt:
        return datetime(*dt[:6])  # Convert time tuple to datetime
    return datetime.now()  # Fallback to current time


class FeedCache:
    """Simple file-based cache for RSS feeds with TTL support."""

    def __init__(
        self, cache_dir: Path | str = "~/.cache/rss_reader", ttl_minutes: int = 60
    ):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.stats = {"hits": 0, "misses": 0}

    def _get_cache_path(self, url: str) -> Path:
        """Get cache file path for a URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self.cache_dir / f"{url_hash}.pkl"

    def get(self, url: str) -> feedparser.FeedParserDict | None:
        """Get cached feed if valid, None otherwise."""
        cache_path = self._get_cache_path(url)

        if not cache_path.exists():
            self.stats["misses"] += 1
            return None

        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)

            # Check TTL
            cached_time = datetime.fromisoformat(cached["timestamp"])
            if datetime.now() - cached_time > self.ttl:
                self.stats["misses"] += 1
                return None

            self.stats["hits"] += 1
            return cached["feed"]
        except Exception:
            self.stats["misses"] += 1
            return None

    def set(self, url: str, feed: feedparser.FeedParserDict) -> None:
        """Cache a feed."""
        cache_path = self._get_cache_path(url)

        try:
            cached = {"timestamp": datetime.now().isoformat(), "url": url, "feed": feed}
            with open(cache_path, "wb") as f:
                pickle.dump(cached, f)
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to cache feed: {e}[/yellow]")

    def clear(self) -> int:
        """Clear all cached feeds. Returns number of files removed."""
        count = 0
        for cache_file in self.cache_dir.glob("*.pkl"):
            cache_file.unlink()
            count += 1
        return count

    def get_stats(self) -> dict[str, int]:
        """Get cache statistics."""
        return self.stats.copy()


def format_entry(
    entry: feedparser.FeedParserDict,
    source: str,
    dt: datetime,
    template: Optional[str],
    date_format: str,
    include_summary: bool = False,
) -> str:
    """Format an RSS entry using a template string.

    Available placeholders:
    - {date}: Entry date
    - {source}: Source name
    - {title}: Entry title
    - {link}: Entry URL
    - {summary}: Entry summary (if available)
    """
    # Default template if none provided
    if template is None:
        template = "{date} [{source}] {title} <{link}>"

    # Prepare values for template
    values = {
        "date": dt.strftime(date_format),
        "source": source,
        "title": entry.title,
        "link": entry.link,
        "summary": entry.get("summary", "") if include_summary else "",
    }

    # Apply template
    try:
        return template.format(**values)
    except KeyError as e:
        console.print(f"[red]Error: Invalid placeholder in format template: {e}[/red]")
        sys.exit(1)


def apply_filters(
    entries: list[tuple[datetime, str, feedparser.FeedParserDict]],
    filter_title: Optional[str],
    filter_source: Optional[str],
) -> list[tuple[datetime, str, feedparser.FeedParserDict]]:
    """Apply filtering to entries based on title and source patterns."""
    import re

    filtered = entries

    # Filter by title
    if filter_title:
        try:
            pattern = re.compile(filter_title, re.IGNORECASE)
            filtered = [
                (dt, src, entry)
                for dt, src, entry in filtered
                if pattern.search(entry.title)
            ]
        except re.error as e:
            console.print(f"[red]Error: Invalid title filter regex: {e}[/red]")
            sys.exit(1)

    # Filter by source
    if filter_source:
        try:
            pattern = re.compile(filter_source, re.IGNORECASE)
            filtered = [
                (dt, src, entry) for dt, src, entry in filtered if pattern.search(src)
            ]
        except re.error as e:
            console.print(f"[red]Error: Invalid source filter regex: {e}[/red]")
            sys.exit(1)

    return filtered


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load RSS feeds configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        console.print(f"[red]Error: Config file not found: {config_path}[/red]")
        sys.exit(1)

    with open(path) as f:
        config: dict[str, Any] = yaml.safe_load(f)
        return config


def get_feeds_for_domain(config: dict[str, Any], domain: str) -> dict[str, str]:
    """Extract feed URLs for a specific domain."""
    domains = config.get("domains", {})
    if domain not in domains:
        console.print(f"[red]Error: Domain '{domain}' not found in config[/red]")
        console.print(f"Available domains: {', '.join(domains.keys())}")
        sys.exit(1)

    # Convert sources list to feeds dict
    feeds = {}
    for source in domains[domain].get("sources", []):
        feeds[source["name"]] = source["url"]
    return feeds


def get_all_feeds(config: dict[str, Any]) -> dict[str, str]:
    """Get all feeds across all domains."""
    all_feeds = {}
    for domain_config in config.get("domains", {}).values():
        for source in domain_config.get("sources", []):
            all_feeds[source["name"]] = source["url"]
    return all_feeds


def get_all_tags(config: dict[str, Any]) -> dict[str, list[str]]:
    """
    Extract all unique tags from config with their associated sources.

    Returns: dict mapping tag -> list of source names
    """
    tag_map: dict[str, list[str]] = {}

    for domain_name, domain_config in config.get("domains", {}).items():
        for source in domain_config.get("sources", []):
            source_name = source["name"]
            keywords = source.get("keywords", [])

            for keyword in keywords:
                tag = keyword.lower()  # Normalize to lowercase
                if tag not in tag_map:
                    tag_map[tag] = []
                if source_name not in tag_map[tag]:
                    tag_map[tag].append(source_name)

    return tag_map


def get_feeds_by_tags(
    config: dict[str, Any], tags: list[str], match_mode: str = "any"
) -> dict[str, str]:
    """
    Filter feeds by tags (keywords).

    Args:
        config: RSS feed configuration
        tags: List of tags to filter by
        match_mode: 'any' (OR) or 'all' (AND) matching

    Returns: dict mapping source name -> URL for matching feeds
    """
    feeds: dict[str, str] = {}
    normalized_tags = [t.lower() for t in tags]

    for domain_name, domain_config in config.get("domains", {}).items():
        for source in domain_config.get("sources", []):
            source_name = source["name"]
            source_url = source["url"]
            source_keywords = [k.lower() for k in source.get("keywords", [])]

            # Check if source matches tags
            if match_mode == "any":
                # Match if ANY tag is present
                if any(tag in source_keywords for tag in normalized_tags):
                    feeds[source_name] = source_url
            elif match_mode == "all":
                # Match if ALL tags are present
                if all(tag in source_keywords for tag in normalized_tags):
                    feeds[source_name] = source_url

    return feeds


def list_tags_info(config: dict[str, Any]) -> str:
    """
    Generate formatted list of all available tags with source counts.

    Returns: Formatted string showing tags and their usage
    """
    tag_map = get_all_tags(config)

    if not tag_map:
        return "No tags found in config"

    # Sort tags by number of sources (descending), then alphabetically
    sorted_tags = sorted(tag_map.items(), key=lambda x: (-len(x[1]), x[0]))

    lines = ["Available Tags:\n"]
    for tag, sources in sorted_tags:
        source_count = len(sources)
        lines.append(
            f"  {tag}: {source_count} source{'s' if source_count != 1 else ''}"
        )
        lines.append(f"    Sources: {', '.join(sources)}")

    return "\n".join(lines)


def validate_url(url: str) -> bool:
    """Validate if string is a valid URL."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def validate_feed(url: str, cache: FeedCache | None = None) -> FeedValidationResult:
    """
    Perform comprehensive feed validation.

    Checks:
    - URL format validity
    - Feed fetchability and parse-ability
    - Feed structure (title, entries)
    - Entry freshness (age of latest entry)
    - Performance (fetch time)

    Returns:
        FeedValidationResult with validation status and details
    """
    result = FeedValidationResult(url=url)

    # 1. URL validation
    if not validate_url(url):
        result.add_error("Invalid URL format")
        return result

    # 2. Fetch and parse with timing
    start_time = time.time()
    try:
        # Check cache first (if provided and --validate-cached not used)
        feed = None
        if cache:
            feed = cache.get(url)
            if feed:
                result.info["cached"] = True

        if not feed:
            feed = feedparser.parse(url)
            result.info["cached"] = False

        fetch_time = time.time() - start_time
        result.info["fetch_time_ms"] = int(fetch_time * 1000)
    except Exception as e:
        result.add_error(f"Failed to fetch feed: {e}")
        return result

    # 3. Parse error check
    if feed.bozo:
        # Some feeds work despite bozo flag, so this is a warning
        result.add_warning(f"Feed has parsing issues: {feed.bozo_exception}")

    # 4. Feed structure validation
    if not hasattr(feed, "feed"):
        result.add_error("Feed missing required 'feed' attribute")
        return result

    # 5. Feed metadata validation
    feed_title = feed.feed.get("title", "")
    if not feed_title:
        result.add_warning("Feed missing title")
    else:
        result.info["title"] = feed_title

    result.info["link"] = feed.feed.get("link", "")

    # 6. Entries validation
    if not feed.entries:
        result.add_warning("Feed has no entries (empty feed)")
        result.info["entry_count"] = 0
    else:
        result.info["entry_count"] = len(feed.entries)

        # Check entry freshness
        latest_entry = feed.entries[0]
        dt = latest_entry.get("updated_parsed") or latest_entry.get("published_parsed")
        if dt:
            dt_obj = datetime(*dt[:6])
            age_days = (datetime.now() - dt_obj).days
            result.info["latest_entry_age_days"] = age_days
            result.info["latest_entry_date"] = dt_obj.isoformat()

            if age_days > 90:
                result.add_warning(f"Latest entry is {age_days} days old (stale feed)")
            elif age_days > 30:
                result.add_warning(
                    f"Latest entry is {age_days} days old (may be stale)"
                )

        # Check for required entry fields
        missing_fields = []
        if not latest_entry.get("title"):
            missing_fields.append("title")
        if not latest_entry.get("link"):
            missing_fields.append("link")

        if missing_fields:
            result.add_warning(f"Entries missing fields: {', '.join(missing_fields)}")

    # 7. Performance check
    fetch_time_ms = result.info.get("fetch_time_ms", 0)
    if fetch_time_ms > 10000:
        result.add_warning(f"Very slow feed response ({fetch_time_ms}ms > 10s)")
    elif fetch_time_ms > 5000:
        result.add_warning(f"Slow feed response ({fetch_time_ms}ms > 5s)")

    return result


def format_validation_result(
    result: FeedValidationResult, verbose: bool = False
) -> str:
    """Format validation result as human-readable text."""
    lines = []

    # Status line
    if result.is_valid:
        if result.warnings:
            status = "[yellow]⚠ VALID WITH WARNINGS[/yellow]"
        else:
            status = "[green]✓ VALID[/green]"
    else:
        status = "[red]✗ INVALID[/red]"

    lines.append(f"{status}: {result.url}")

    # Info
    if verbose and result.info:
        lines.append("\nInfo:")
        for key, value in result.info.items():
            lines.append(f"  {key}: {value}")

    # Errors
    if result.errors:
        lines.append("\nErrors:")
        for error in result.errors:
            lines.append(f"  • {error}")

    # Warnings
    if result.warnings:
        lines.append("\nWarnings:")
        for warning in result.warnings:
            lines.append(f"  • {warning}")

    return "\n".join(lines)


def fetch_feed_safe(
    source_name: str, url: str, cache: FeedCache | None = None
) -> tuple[str, feedparser.FeedParserDict | None]:
    """Fetch a single feed safely, returning (source_name, feed or None). Uses cache if provided."""
    # Check cache first
    if cache:
        cached_feed = cache.get(url)
        if cached_feed:
            return source_name, cached_feed

    try:
        if not validate_url(url):
            console.print(
                f"[yellow]Warning: Invalid URL for {source_name}: {url}[/yellow]"
            )
            return source_name, None

        feed = feedparser.parse(url)
        if feed.bozo:
            console.print(
                f"[yellow]Warning: Failed to parse {source_name}: {feed.bozo_exception}[/yellow]"
            )
            return source_name, None

        # Cache successful fetch
        if cache:
            cache.set(url, feed)

        return source_name, feed
    except Exception as e:
        console.print(f"[yellow]Warning: Error fetching {source_name}: {e}[/yellow]")
        return source_name, None


def fetch_feeds_parallel(
    feeds: dict[str, str], cache: FeedCache | None = None, max_workers: int = 5
) -> dict[str, feedparser.FeedParserDict]:
    """Fetch multiple feeds in parallel using ThreadPoolExecutor. Uses cache if provided."""
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all fetch tasks
        future_to_source = {
            executor.submit(fetch_feed_safe, source_name, url, cache): source_name
            for source_name, url in feeds.items()
        }

        for future in as_completed(future_to_source):
            source_name, feed = future.result()
            if feed is not None:
                results[source_name] = feed

    return results


def deduplicate_entries(
    entries: list[tuple[str, feedparser.FeedParserDict]],
) -> list[tuple[str, feedparser.FeedParserDict]]:
    """Deduplicate entries by URL, keeping first occurrence."""
    seen_urls = set()
    unique_entries = []

    for source, entry in entries:
        url = entry.link
        if url not in seen_urls:
            seen_urls.add(url)
            unique_entries.append((source, entry))

    return unique_entries


def fetch_feed(url: str, cache: FeedCache | None = None) -> feedparser.FeedParserDict:
    """Fetch and parse RSS feed. Uses cache if provided."""
    # Check cache first
    if cache:
        cached_feed = cache.get(url)
        if cached_feed:
            return cached_feed

    if not validate_url(url):
        console.print(f"[red]Error: Invalid URL: {url}[/red]")
        sys.exit(1)

    feed = feedparser.parse(url)
    if feed.bozo:  # feedparser sets this flag for malformed feeds
        console.print(f"[red]Error: Failed to parse feed: {feed.bozo_exception}[/red]")
        sys.exit(1)

    # Cache successful fetch
    if cache:
        cache.set(url, feed)

    return feed


def get_entry_summary(entry: feedparser.FeedParserDict) -> str | None:
    """Extract and clean summary from entry."""
    # Try to get the best content available
    if "link" in entry and "github.com" in entry.link and "/blob/" not in entry.link:
        # For GitHub repos, try to get the repository description
        if "description" in entry:
            summary = entry.description
        else:
            summary = "A GitHub repository: " + entry.link.split("github.com/")[-1]
    elif "content" in entry and entry.content:
        summary = entry.content[0].value
    else:
        summary = entry.get("summary", "") or entry.get("description", "") or ""

    # Remove HTML tags more thoroughly
    # Remove any HTML tag
    summary = re.sub(r"<[^>]+>", " ", summary)
    # Decode all HTML entities
    summary = html.unescape(summary)

    # Clean up whitespace
    summary = " ".join(summary.split())

    # Skip if summary doesn't meet quality criteria
    if (
        not summary
        or len(summary) < 20
        or "comments" in summary.lower()
        or summary.startswith("http")
    ):
        return None

    # Truncate if too long
    if len(summary) > 300:
        summary = summary[:297] + "..."

    return str(summary)


def format_entries(
    feed: feedparser.FeedParserDict,
    exclude_urls: Optional[list[str]] = None,
    max_entries: Optional[int] = None,
    include_summary: bool = False,
    time: bool = False,
    order: Literal["asc", "desc"] = "asc",
    format_template: Optional[str] = None,
    date_format: str = "%Y-%m-%d",
    filter_title: Optional[str] = None,
    filter_source: Optional[str] = None,
) -> str:
    """Format feed entries in a clean, LLM-friendly format."""
    # Get feed title for source name
    source = feed.feed.get("title", "Unknown")

    # Convert entries to (datetime, source, entry) tuples
    dt_entries = []
    for entry in feed.entries:
        # Use feedparser's built-in date parsing
        dt = entry.get("updated_parsed") or entry.get("published_parsed")
        if dt:
            dt = datetime(*dt[:6])  # Convert time tuple to datetime
        else:
            dt = datetime.now()  # Fallback to current time
        dt_entries.append((dt, source, entry))

    # Sort by date
    dt_entries.sort(key=lambda x: x[0], reverse=True)
    if order == "asc":
        dt_entries = list(reversed(dt_entries))

    # Apply max_entries limit
    if max_entries:
        if order == "asc":
            dt_entries = dt_entries[:max_entries]
        else:
            # descending: take first N
            dt_entries = dt_entries[:max_entries]

    # Apply exclude_urls filter
    if exclude_urls:
        dt_entries = [
            (dt, src, entry)
            for dt, src, entry in dt_entries
            if entry.link not in exclude_urls
        ]

    # Apply custom filters
    dt_entries = apply_filters(dt_entries, filter_title, filter_source)

    # Handle time parameter (overrides date_format)
    if time and not format_template:
        date_format = "%Y-%m-%d %H:%M"

    # Format output
    output = []
    for dt, src, entry in dt_entries:
        formatted = format_entry(
            entry, src, dt, format_template, date_format, include_summary
        )
        output.append(formatted)

    return "\n".join(output)


def discover_feeds(url: str, timeout: int = 10) -> list[str]:
    """
    Discover RSS/Atom feeds from a website URL.

    Checks for feed links in HTML <link> tags and tries common feed locations.

    Args:
        url: Website URL to search for feeds
        timeout: Request timeout in seconds

    Returns:
        List of discovered feed URLs (empty if none found)
    """
    discovered = []

    # Ensure URL has scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        # Fetch HTML
        response = requests.get(
            url, timeout=timeout, headers={"User-Agent": "gptme-rss-reader/1.0"}
        )
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.content, "html.parser")

        # Find feed links in <link rel="alternate"> tags
        for link in soup.find_all("link", rel="alternate"):
            feed_type = link.get("type", "")
            if "rss" in feed_type or "atom" in feed_type or "feed" in feed_type:
                href = link.get("href")
                if href:
                    # Make absolute URL
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        parsed = urlparse(url)
                        href = f"{parsed.scheme}://{parsed.netloc}{href}"
                    elif not href.startswith("http"):
                        # Relative URL
                        href = url.rstrip("/") + "/" + href.lstrip("/")

                    if href not in discovered:
                        discovered.append(href)

        # If no feeds found, try common feed locations
        if not discovered:
            common_paths = [
                "/feed",
                "/rss",
                "/atom.xml",
                "/feed.xml",
                "/rss.xml",
                "/index.xml",
                "/feeds/posts/default",
                "/blog/feed",
            ]

            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            for path in common_paths:
                feed_url = base_url + path
                try:
                    # Quick check if URL returns valid feed
                    feed_response = requests.head(feed_url, timeout=5)
                    if feed_response.status_code == 200:
                        content_type = feed_response.headers.get("content-type", "")
                        if (
                            "xml" in content_type
                            or "rss" in content_type
                            or "atom" in content_type
                        ):
                            if feed_url not in discovered:
                                discovered.append(feed_url)
                except requests.RequestException:
                    pass  # Skip failed attempts

    except requests.RequestException as e:
        console.print(f"[yellow]Warning: Failed to fetch {url}: {e}[/yellow]")

    return discovered


def format_multi_feed_output(
    feeds: dict[str, feedparser.FeedParserDict],
    config: dict[str, Any],
    max_entries: Optional[int] = None,
    include_summary: bool = False,
    format_template: Optional[str] = None,
    date_format: str = "%Y-%m-%d",
    filter_title: Optional[str] = None,
    filter_source: Optional[str] = None,
) -> str:
    """Format output for multiple feeds, grouped by domain."""
    output = []

    # Group feeds by domain
    domain_feeds: dict[str, list[tuple[str, feedparser.FeedParserDict]]] = {}
    for source_name, feed in feeds.items():
        # Find which domain this source belongs to
        domain_name = None
        for d_name, d_config in config.get("domains", {}).items():
            for source in d_config.get("sources", []):
                if source["name"] == source_name:
                    domain_name = d_name
                    break
            if domain_name:
                break

        if domain_name:
            if domain_name not in domain_feeds:
                domain_feeds[domain_name] = []
            domain_feeds[domain_name].append((source_name, feed))

    # Collect all entries with source info
    all_entries: list[tuple[str, feedparser.FeedParserDict]] = []
    for domain_name, sources in domain_feeds.items():
        for source_name, feed in sources:
            for entry in feed.entries:
                all_entries.append((source_name, entry))

    # Deduplicate
    all_entries = deduplicate_entries(all_entries)

    # Sort by date (newest first)
    dt_entries = []
    for source, entry in all_entries:
        dt = entry.get("updated_parsed") or entry.get("published_parsed")
        if dt:
            dt = datetime(*dt[:6])
        else:
            dt = datetime.now()
        dt_entries.append((dt, source, entry))
    dt_entries.sort(key=lambda x: x[0], reverse=True)

    # Apply max_entries limit
    if max_entries:
        dt_entries = dt_entries[:max_entries]

    # Apply filters
    dt_entries = apply_filters(dt_entries, filter_title, filter_source)

    # Format output
    for dt, source, entry in dt_entries:
        formatted = format_entry(
            entry,
            source,
            dt,
            format_template,
            date_format,
            include_summary=include_summary,
        )
        output.append(formatted)

        if include_summary:
            summary = get_entry_summary(entry)
            if summary:
                output.append(f"  Summary: {summary}")

    return "\n".join(output)


def format_by_tag_groups(
    all_entries: list[tuple[str, feedparser.FeedParserDict]],
    config: dict[str, Any],
    max_entries: Optional[int] = None,
    include_summary: bool = False,
    format_template: Optional[str] = None,
    date_format: str = "%Y-%m-%d",
    filter_title: Optional[str] = None,
    filter_source: Optional[str] = None,
) -> str:
    """
    Group and format entries by tags instead of domains.

    Args:
        all_entries: List of (source_name, entry) tuples
        config: RSS feed configuration
        max_entries: Maximum entries per tag
        include_summary: Include entry summaries
        format_template: Custom format template
        date_format: Date format string
        filter_title: Filter by title regex
        filter_source: Filter by source regex

    Returns: Formatted output grouped by tags
    """
    # Build source -> tags mapping
    source_tags: dict[str, list[str]] = {}
    for domain_name, domain_config in config.get("domains", {}).items():
        for source in domain_config.get("sources", []):
            source_name = source["name"]
            keywords = [k.lower() for k in source.get("keywords", [])]
            source_tags[source_name] = keywords if keywords else ["untagged"]

    # Group entries by tag
    tag_entries: dict[str, list[tuple[datetime, str, feedparser.FeedParserDict]]] = {}
    for source_name, entry in all_entries:
        # Get datetime for entry
        dt = entry.get("updated_parsed") or entry.get("published_parsed")
        if dt:
            dt = datetime(*dt[:6])
        else:
            dt = datetime.now()

        # Add entry to all tags for this source
        tags = source_tags.get(source_name, ["untagged"])
        for tag in tags:
            if tag not in tag_entries:
                tag_entries[tag] = []
            tag_entries[tag].append((dt, source_name, entry))

    # Sort entries within each tag by date (newest first)
    for tag in tag_entries:
        tag_entries[tag].sort(key=lambda x: x[0], reverse=True)

    # Format output
    output = []
    for tag in sorted(tag_entries.keys()):
        entries = tag_entries[tag]

        # Apply max_entries limit per tag
        if max_entries:
            entries = entries[:max_entries]

        # Convert to format expected by apply_filters
        entries_for_filter = [(dt, source, entry) for dt, source, entry in entries]
        entries_for_filter = apply_filters(
            entries_for_filter, filter_title, filter_source
        )

        if not entries_for_filter:
            continue

        output.append(f"\n=== Tag: {tag} ({len(entries_for_filter)} entries) ===\n")

        for dt, source_name, entry in entries_for_filter:
            formatted = format_entry(
                entry,
                source_name,
                dt,
                format_template,
                date_format,
                include_summary=include_summary,
            )
            output.append(formatted)

            if include_summary:
                summary = get_entry_summary(entry)
                if summary:
                    output.append(f"  Summary: {summary}")

    return "\n".join(output)


@click.command()
@click.argument("url", required=False)
@click.option(
    "--exclude-url", "-e", multiple=True, help="URL patterns to exclude from output"
)
@click.option(
    "--max-entries", "-n", type=int, help="Maximum number of entries to display"
)
@click.option("--summary", "-s", is_flag=True, help="Include entry summaries")
@click.option("--time", "-t", is_flag=True, help="Include time in date format")
@click.option(
    "--order",
    type=click.Choice(["asc", "desc"]),
    default="asc",
    help="Entry order (asc=oldest first)",
)
@click.option("--json", "-j", "json_output", is_flag=True, help="Output in JSON format")
@click.option("--config", "-c", type=str, help="Path to RSS feeds config file (YAML)")
@click.option(
    "--domain", "-d", type=str, help="Domain to fetch feeds for (requires --config)"
)
@click.option(
    "--all-domains",
    "-a",
    is_flag=True,
    help="Fetch feeds from all domains in config (requires --config)",
)
@click.option("--no-cache", is_flag=True, help="Bypass cache and fetch fresh feeds")
@click.option(
    "--clear-cache", is_flag=True, help="Clear all cached feeds before running"
)
@click.option(
    "--cache-ttl", type=int, default=60, help="Cache TTL in minutes (default: 60)"
)
@click.option(
    "--cache-dir", type=str, default="~/.cache/rss_reader", help="Cache directory path"
)
@click.option("--show-cache-stats", is_flag=True, help="Show cache hit/miss statistics")
@click.option("--validate", is_flag=True, help="Validate feed health and report issues")
@click.option(
    "--validate-all",
    is_flag=True,
    help="Validate all feeds in config (requires --config)",
)
@click.option(
    "--validate-verbose", "-v", is_flag=True, help="Show detailed validation info"
)
@click.option(
    "--discover", is_flag=True, help="Discover RSS/Atom feeds from a website URL"
)
@click.option(
    "--format",
    "-f",
    type=str,
    help="Custom output format template (e.g., '{date} {title} <{link}>')",
)
@click.option(
    "--date-format",
    type=str,
    default="%Y-%m-%d",
    help="Custom date format (strftime, default: %Y-%m-%d)",
)
@click.option(
    "--filter-title", type=str, help="Filter entries by title (regex pattern)"
)
@click.option(
    "--filter-source", type=str, help="Filter entries by source name (regex pattern)"
)
@click.option(
    "--tags", type=str, help="Filter feeds by tags (comma-separated, requires --config)"
)
@click.option(
    "--tag-match",
    type=click.Choice(["any", "all"]),
    default="any",
    help="Tag matching mode: any (OR) or all (AND)",
)
@click.option(
    "--list-tags",
    is_flag=True,
    help="List all available tags from config (requires --config)",
)
@click.option(
    "--group-by",
    type=click.Choice(["domain", "tag", "source"]),
    default="domain",
    help="Group output by domain, tag, or source",
)
@click.option(
    "--search", "-S", type=str, help="Search entries by query (regex pattern)"
)
@click.option(
    "--search-in",
    type=click.Choice(["title", "summary", "link", "all"]),
    default="all",
    help="Field to search in (default: all)",
)
@click.option(
    "--search-history",
    "search_history_flag",
    is_flag=True,
    help="Show recent search queries",
)
@click.option(
    "--save-search/--no-save-search",
    default=True,
    help="Save search to history (default: true)",
)
def main(
    url: Optional[str],
    exclude_url: tuple[str, ...],
    max_entries: Optional[int],
    summary: bool,
    time: bool,
    order: str,
    json_output: bool,
    config: Optional[str],
    domain: Optional[str],
    all_domains: bool,
    no_cache: bool,
    clear_cache: bool,
    cache_ttl: int,
    cache_dir: str,
    show_cache_stats: bool,
    validate: bool,
    validate_all: bool,
    validate_verbose: bool,
    discover: bool,
    format: Optional[str],
    date_format: str,
    filter_title: Optional[str],
    filter_source: Optional[str],
    tags: Optional[str],
    tag_match: str,
    list_tags: bool,
    group_by: str,
    search: Optional[str],
    search_in: str,
    search_history_flag: bool,
    save_search: bool,
) -> None:
    """
    Read RSS feeds and display them in a compact format.

    If URL is not provided, uses RSS_URL from environment or config file.

    Examples:

        # Read single feed
        rss_reader.py https://news.ycombinator.com/rss

        # Read with summaries and limit entries
        rss_reader.py https://news.ycombinator.com/rss --summary --max-entries 5

        # Read from config (specific domain)
        rss_reader.py --config feeds.yaml --domain ai

        # Read all domains from config
        rss_reader.py --config feeds.yaml --all-domains

        # Validate a feed
        rss_reader.py https://example.com/feed.xml --validate

        # Validate all feeds in config
        rss_reader.py --config feeds.yaml --validate-all
    """
    # Setup cache (even if --no-cache, we need it for stats)
    cache = (
        FeedCache(cache_dir=cache_dir, ttl_minutes=cache_ttl) if not no_cache else None
    )

    # Clear cache if requested
    if clear_cache and cache:
        cleared = cache.clear()
        console.print(f"Cleared {cleared} cached feed(s)")
        if not (url or config):  # If just clearing cache, exit
            return

    # Discovery mode
    if discover:
        if not url:
            console.print("[red]Error: --discover requires a website URL[/red]")
            sys.exit(1)

        console.print(f"[cyan]Discovering feeds from {url}...[/cyan]\n")
        discovered_feeds = discover_feeds(url)

        if discovered_feeds:
            console.print(f"[green]Found {len(discovered_feeds)} feed(s):[/green]\n")
            for i, feed_url in enumerate(discovered_feeds, 1):
                console.print(f"  {i}. {feed_url}")

            # Optionally validate discovered feeds
            if validate or validate_verbose:
                console.print("\n[cyan]Validating discovered feeds...[/cyan]\n")
                for feed_url in discovered_feeds:
                    result = validate_feed(feed_url, cache=None)
                    formatted = format_validation_result(
                        result, verbose=validate_verbose
                    )
                    console.print(formatted)
                    console.print("")
        else:
            console.print("[yellow]No feeds discovered[/yellow]")

        return

    # Validation mode
    if validate or validate_all:
        if validate_all:
            if not config:
                console.print("[red]Error: --validate-all requires --config[/red]")
                sys.exit(1)

            cfg = load_config(config)
            feeds = get_all_feeds(cfg)

            console.print(f"\n[bold]Validating {len(feeds)} feeds...[/bold]\n")

            results = []
            for source_name, feed_url in feeds.items():
                result = validate_feed(
                    feed_url, cache=None
                )  # Don't use cache for validation
                results.append((source_name, result))

            # Create summary table
            table = Table(title="Feed Validation Summary")
            table.add_column("Feed", style="cyan")
            table.add_column("Status", style="bold")
            table.add_column("Entries", justify="right")
            table.add_column("Age (days)", justify="right")
            table.add_column("Fetch (ms)", justify="right")
            table.add_column("Issues", justify="right")

            for source_name, result in results:
                if result.is_valid:
                    if result.warnings:
                        status = "[yellow]⚠ WARN[/yellow]"
                    else:
                        status = "[green]✓ OK[/green]"
                else:
                    status = "[red]✗ FAIL[/red]"

                entries = str(result.info.get("entry_count", "-"))
                age = str(result.info.get("latest_entry_age_days", "-"))
                fetch_time = str(result.info.get("fetch_time_ms", "-"))
                issues = len(result.errors) + len(result.warnings)

                table.add_row(
                    source_name,
                    status,
                    entries,
                    age,
                    fetch_time,
                    str(issues) if issues > 0 else "-",
                )

            console.print(table)

            # Show detailed results if verbose
            if validate_verbose:
                console.print("\n[bold]Detailed Results:[/bold]\n")
                for source_name, result in results:
                    console.print(f"\n[bold]{source_name}[/bold]")
                    formatted = format_validation_result(result, verbose=True)
                    console.print(formatted)
                    console.print("")

        else:
            # Validate single feed
            if not url:
                url = os.getenv("RSS_URL")
                if not url:
                    console.print(
                        "[red]Error: No URL provided and RSS_URL not set[/red]"
                    )
                    sys.exit(1)

            result = validate_feed(url, cache=None)
            formatted = format_validation_result(result, verbose=validate_verbose)
            console.print(formatted)

            if not result.is_valid:
                sys.exit(1)

        return

    # Show search history if requested
    if search_history_flag:
        history = load_search_history(cache_dir)
        console.print(format_search_history(history))
        return

    # Multi-feed mode (config-based)
    if config:
        cfg = load_config(config)

        # Handle --list-tags
        if list_tags:
            tags_info = list_tags_info(cfg)
            console.print(tags_info)
            return

        # Handle --tags filtering
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            feeds = get_feeds_by_tags(cfg, tag_list, tag_match)
            if not feeds:
                console.print(
                    f"[yellow]No feeds found matching tags: {', '.join(tag_list)} (match mode: {tag_match})[/yellow]"
                )
                return
        elif all_domains:
            feeds = get_all_feeds(cfg)
        elif domain:
            feeds = get_feeds_for_domain(cfg, domain)
        else:
            console.print(
                "[red]Error: --config requires either --domain, --all-domains, or --tags[/red]"
            )
            sys.exit(1)

        console.print(f"Fetching {len(feeds)} feeds...")
        fetched_feeds = fetch_feeds_parallel(feeds, cache=cache)
        console.print(f"Successfully fetched {len(fetched_feeds)}/{len(feeds)} feeds")

        if json_output:
            # JSON output for multi-feed
            output_data = {}
            for source_name, feed in fetched_feeds.items():
                output_data[source_name] = {
                    "title": feed.feed.get("title", ""),
                    "link": feed.feed.get("link", ""),
                    "entries": [
                        {
                            "title": entry.title,
                            "link": entry.link,
                            "published": entry.get("published", ""),
                        }
                        for entry in (
                            feed.entries[:max_entries] if max_entries else feed.entries
                        )
                    ],
                }
            console.print(json_lib.dumps(output_data, indent=2))
        else:
            # Text output with grouping
            if group_by == "tag":
                # Convert fetched_feeds to list of (source_name, entry) tuples
                all_entries = []
                for source_name, feed in fetched_feeds.items():
                    for entry in feed.entries:
                        # Convert to (datetime, source, entry) tuple format
                        pub_time = parse_date(entry)
                        all_entries.append((pub_time, source_name, entry))

                # Apply search if specified
                if search:
                    original_count = len(all_entries)
                    all_entries = search_entries(all_entries, search, search_in)
                    if save_search:
                        save_to_search_history(
                            search, search_in, len(all_entries), cache_dir
                        )
                    console.print(
                        f"[dim]Found {len(all_entries)}/{original_count} entries matching '{search}' in {search_in}[/dim]\n"
                    )

                # Convert back to (source, entry) format for format_by_tag_groups
                entries_for_format = [
                    (source, entry) for dt, source, entry in all_entries
                ]
                output = format_by_tag_groups(
                    entries_for_format,
                    cfg,
                    max_entries,
                    summary,
                    format,
                    date_format,
                    filter_title,
                    filter_source,
                )
            else:
                # Domain or source grouping (existing behavior)
                # Apply search to fetched feeds if specified
                if search:
                    # Convert to entry list, apply search, convert back
                    all_entries = []
                    for source_name, feed in fetched_feeds.items():
                        for entry in feed.entries:
                            pub_time = parse_date(entry)
                            all_entries.append((pub_time, source_name, entry))

                    original_count = len(all_entries)
                    all_entries = search_entries(all_entries, search, search_in)

                    if save_search:
                        save_to_search_history(
                            search, search_in, len(all_entries), cache_dir
                        )

                    console.print(
                        f"[dim]Found {len(all_entries)}/{original_count} entries matching '{search}' in {search_in}[/dim]\n"
                    )

                    # Convert back to fetched_feeds structure
                    filtered_feeds = {}
                    for dt, source_name, entry in all_entries:
                        if source_name not in filtered_feeds:
                            # Create a new feed object with filtered entries
                            original_feed = fetched_feeds[source_name]
                            filtered_feed = feedparser.FeedParserDict()
                            filtered_feed.feed = original_feed.feed
                            filtered_feed.entries = []
                            filtered_feeds[source_name] = filtered_feed
                        filtered_feeds[source_name].entries.append(entry)

                    fetched_feeds = filtered_feeds

                output = format_multi_feed_output(
                    fetched_feeds,
                    cfg,
                    max_entries,
                    summary,
                    format,
                    date_format,
                    filter_title,
                    filter_source,
                )
            console.print(output)

        if show_cache_stats and cache:
            stats = cache.get_stats()
            total = stats["hits"] + stats["misses"]
            hit_rate = (stats["hits"] / total * 100) if total > 0 else 0
            console.print(
                f"\nCache: {stats['hits']} hits, {stats['misses']} misses ({hit_rate:.1f}% hit rate)"
            )

        return

    # Single-feed mode
    if not url:
        url = os.getenv("RSS_URL")
        if not url:
            console.print("[red]Error: No URL provided and RSS_URL not set[/red]")
            sys.exit(1)

    feed = fetch_feed(url, cache=cache)

    if json_output:
        # JSON output
        output = json_lib.dumps(
            {
                "title": feed.feed.get("title", ""),
                "link": feed.feed.get("link", ""),
                "entries": [
                    {
                        "title": entry.title,
                        "link": entry.link,
                        "published": entry.get("published", ""),
                    }
                    for entry in (
                        feed.entries[:max_entries] if max_entries else feed.entries
                    )
                ],
            },
            indent=2,
        )
        console.print(output)
    else:
        # Text output
        from typing import cast

        # Apply search if specified
        if search:
            # Convert entries to searchable format
            searchable_entries: list[tuple[datetime, str, Any]] = []
            for entry in feed.entries:
                pub_time = parse_date(entry)
                searchable_entries.append((pub_time, "feed", entry))

            original_count = len(searchable_entries)
            searchable_entries = search_entries(searchable_entries, search, search_in)

            if save_search:
                save_to_search_history(
                    search, search_in, len(searchable_entries), cache_dir
                )

            console.print(
                f"[dim]Found {len(searchable_entries)}/{original_count} entries matching '{search}' in {search_in}[/dim]\n"
            )

            # Convert back to feed structure
            feed.entries = [entry for _, _, entry in searchable_entries]

        output = format_entries(
            feed,
            list(exclude_url),
            max_entries,
            summary,
            time,
            cast(Literal["asc", "desc"], order),
            format,
            date_format,
            filter_title,
            filter_source,
        )
        console.print(output)

    if show_cache_stats and cache:
        stats = cache.get_stats()
        total = stats["hits"] + stats["misses"]
        hit_rate = (stats["hits"] / total * 100) if total > 0 else 0
        console.print(
            f"\nCache: {stats['hits']} hits, {stats['misses']} misses ({hit_rate:.1f}% hit rate)"
        )


if __name__ == "__main__":
    main()
