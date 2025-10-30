#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "feedparser>=6.0.0",
#   "click>=8.0.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "pyyaml>=6.0.0",
# ]
# [tool.uv]
# exclude-newer = "2024-01-23T00:00:00Z"
# ///
"""
RSS reader tool for gptme-bob.

Reads RSS feeds and displays them in a compact format.
"""

import html
import json as json_lib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import click
import feedparser
import yaml
from dotenv import load_dotenv
from rich.console import Console

# Load environment variables from .env file
load_dotenv()

console = Console()


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


def fetch_feed_safe(source_name: str, url: str) -> tuple[str, feedparser.FeedParserDict | None]:
    """Fetch a single feed safely, returning (source_name, feed or None)."""
    try:
        if not validate_url(url):
            console.print(f"[yellow]Warning: Invalid URL for {source_name}: {url}[/yellow]")
            return source_name, None

        feed = feedparser.parse(url)
        if feed.bozo:
            console.print(f"[yellow]Warning: Failed to parse {source_name}: {feed.bozo_exception}[/yellow]")
            return source_name, None

        return source_name, feed
    except Exception as e:
        console.print(f"[yellow]Warning: Error fetching {source_name}: {e}[/yellow]")
        return source_name, None


def fetch_feeds_parallel(feeds: dict[str, str], max_workers: int = 5) -> dict[str, feedparser.FeedParserDict]:
    """Fetch multiple feeds in parallel using ThreadPoolExecutor."""
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_source = {executor.submit(fetch_feed_safe, name, url): name for name, url in feeds.items()}

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


def validate_url(url: str) -> bool:
    """Validate if string is a valid URL."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch and parse RSS feed."""
    if not validate_url(url):
        console.print(f"[red]Error: Invalid URL: {url}[/red]")
        sys.exit(1)

    feed = feedparser.parse(url)
    if feed.bozo:  # feedparser sets this flag for malformed feeds
        console.print(f"[red]Error: Failed to parse feed: {feed.bozo_exception}[/red]")
        sys.exit(1)
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
    if not summary or len(summary) < 20 or "comments" in summary.lower() or summary.startswith("http"):
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
) -> str:
    """Format feed entries in a clean, LLM-friendly format."""
    dt_entries = []
    for entry in feed.entries:
        # Use feedparser's built-in date parsing
        dt = entry.get("updated_parsed") or entry.get("published_parsed")
        if dt:
            dt = datetime(*dt[:6])  # Convert time tuple to datetime
        else:
            dt = datetime.now()  # Fallback to current time
        dt_entries.append((dt, entry))
    dt_entries.sort(key=lambda x: x[0], reverse=True)
    if order == "asc":
        dt_entries = list(reversed(dt_entries))

    if max_entries:
        if order == "asc":
            dt_entries = dt_entries[-max_entries:]
        else:
            dt_entries = dt_entries[:max_entries]

    lines: list[str] = []
    for dt, entry in dt_entries:
        # Skip if URL contains any excluded patterns
        if exclude_urls and any(pattern in entry.link for pattern in exclude_urls):
            continue

        # Get date, fallback to current date if not available
        date_str = dt.strftime("%Y-%m-%d")
        if time:  # Add time if requested
            date_str += " " + dt.strftime("%H:%M")

        # Prepare entry lines
        entry_lines = []
        entry_lines.append(f"{date_str} {entry.title.strip()} <{entry.link}>")

        # Add summary if available and requested
        if include_summary:
            summary = get_entry_summary(entry)
            if summary:
                entry_lines.append(f"  Summary: {summary}")

        # Only add blank line if we have previous content and current entry has content
        if lines and entry_lines:
            lines.append("")

        # Add entry lines
        lines.extend(entry_lines)

    return "\n".join(lines)


def format_multi_feed_output(
    feeds_by_domain: dict[str, dict[str, feedparser.FeedParserDict]],
    exclude_urls: Optional[list[str]] = None,
    max_entries: Optional[int] = None,
    include_summary: bool = False,
    time: bool = False,
) -> str:
    """Format aggregated entries from multiple feeds with domain grouping."""
    all_entries = []

    # Collect all entries with their source
    for domain, feeds in feeds_by_domain.items():
        for source_name, feed in feeds.items():
            for entry in feed.entries:
                # Skip excluded URLs
                if exclude_urls and any(pattern in entry.link for pattern in exclude_urls):
                    continue

                all_entries.append((domain, source_name, entry))

    # Deduplicate by URL
    seen_urls = set()
    unique_entries = []
    for domain, source, entry in all_entries:
        if entry.link not in seen_urls:
            seen_urls.add(entry.link)
            unique_entries.append((domain, source, entry))

    # Sort by date
    def get_entry_date(item):
        _, _, entry = item
        return entry.get("published_parsed") or entry.get("updated_parsed") or datetime.now().timetuple()

    unique_entries.sort(key=get_entry_date, reverse=True)

    # Apply max_entries limit
    if max_entries:
        unique_entries = unique_entries[:max_entries]

    # Group by domain for output
    domain_entries: dict[str, list[tuple[str, feedparser.FeedParserDict]]] = {}
    for domain, source, entry in unique_entries:
        if domain not in domain_entries:
            domain_entries[domain] = []
        domain_entries[domain].append((source, entry))

    # Format output
    lines = []
    for domain, entries in domain_entries.items():
        lines.append(f"\n=== {domain.replace('_', ' ').title()} ({len(entries)} entries) ===\n")

        for source, entry in entries:
            # Get date
            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
            dt = datetime(*pub_time[:6]) if pub_time else datetime.now()
            date_str = dt.strftime("%Y-%m-%d")
            if time:
                date_str += " " + dt.strftime("%H:%M")

            # Format entry
            lines.append(f"{date_str} {entry.title.strip()} <{entry.link}>")

            if include_summary:
                summary = get_entry_summary(entry)
                if summary:
                    lines.append(f"  Summary: {summary}")

    # Add statistics
    lines.append(f"\nTotal: {len(unique_entries)} unique entries across {len(domain_entries)} domains")

    return "\n".join(lines)


@click.command()
@click.argument("url", required=False)
@click.option("--config", type=click.Path(exists=True), help="Path to RSS feeds YAML configuration")
@click.option("--domain", type=str, help="Scan specific domain from config")
@click.option("--all-domains", is_flag=True, help="Scan all domains from config")
@click.option("--exclude-url", "-e", multiple=True, help="URLs patterns to exclude")
@click.option("--max-entries", "-n", type=int, help="Maximum number of entries to show")
@click.option("--json-output", is_flag=True, help="Output in JSON format")
@click.option("--summary/--no-summary", default=False, help="Include entry summaries/content")
@click.option("--time", is_flag=True, help="Include time in text output")
def main(
    url: Optional[str],
    config: Optional[str],
    domain: Optional[str],
    all_domains: bool,
    exclude_url: tuple[str],
    max_entries: Optional[int],
    json_output: bool,
    summary: bool,
    time: bool,
) -> None:
    """Read RSS feed(s) and display in compact format.

    URL: Single feed URL. If not provided, uses RSS_URL environment variable.

    Multi-feed mode: Use --config with --domain or --all-domains to scan multiple feeds.
    """
    # Multi-feed mode: config with domain or all-domains
    if config and (domain or all_domains):
        cfg = load_config(config)

        # Get feeds based on mode
        if all_domains:
            feeds_by_domain = {}
            for domain_name in cfg.get("domains", {}).keys():
                feeds_by_domain[domain_name] = get_feeds_for_domain(cfg, domain_name)
        else:
            # domain is guaranteed to be not None here (checked in condition)
            assert domain is not None
            feeds_by_domain = {domain: get_feeds_for_domain(cfg, domain)}

        # Fetch all feeds in parallel
        console.print(f"[cyan]Fetching {sum(len(feeds) for feeds in feeds_by_domain.values())} feeds...[/cyan]")
        all_feeds = {}
        for domain_name, feeds in feeds_by_domain.items():
            all_feeds.update(feeds)

        fetched_feeds = fetch_feeds_parallel(all_feeds)
        console.print(f"[green]Successfully fetched {len(fetched_feeds)} feeds[/green]")

        # Organize by domain
        feeds_by_domain_fetched = {}
        for domain_name, feeds in feeds_by_domain.items():
            feeds_by_domain_fetched[domain_name] = {
                name: fetched_feeds[name] for name in feeds.keys() if name in fetched_feeds
            }

        # Format and output
        output = format_multi_feed_output(feeds_by_domain_fetched, list(exclude_url), max_entries, summary, time=time)
        print(output)
        return

    # Single-feed mode (backward compatible)
    feed_url = url or os.getenv("RSS_URL")
    if not feed_url:
        console.print("[red]Error: No URL provided and RSS_URL environment variable not set[/red]")
        sys.exit(1)

    feed = fetch_feed(feed_url)

    if json_output:
        entries = []
        for entry in feed.entries[:max_entries]:
            if exclude_url and any(pattern in entry.link for pattern in exclude_url):
                continue
            entry_data = {
                "title": entry.title,
                "link": entry.link,
                "date": entry.get("published", datetime.now().isoformat()),
            }
            if summary:
                clean_summary = get_entry_summary(entry)
                if clean_summary:
                    entry_data["summary"] = clean_summary
            entries.append(entry_data)
        print(json_lib.dumps(entries, indent=2))
        return

    # Print formatted entries
    output = format_entries(feed, list(exclude_url), max_entries, summary, time=time)
    print(output)


if __name__ == "__main__":
    main()
