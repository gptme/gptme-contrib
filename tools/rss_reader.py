#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "feedparser>=6.0.0",
#   "click>=8.0.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
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
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlparse

import click
import feedparser
from dotenv import load_dotenv
from rich.console import Console

# Load environment variables from .env file
load_dotenv()

console = Console()


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


@click.command()
@click.argument("url", required=False)
@click.option("--exclude-url", "-e", multiple=True, help="URLs patterns to exclude")
@click.option("--max-entries", "-n", type=int, help="Maximum number of entries to show")
@click.option("--json-output", is_flag=True, help="Output in JSON format")
@click.option("--summary/--no-summary", default=False, help="Include entry summaries/content")
@click.option("--time", is_flag=True, help="Include time in text output")
def main(
    url: Optional[str],
    exclude_url: tuple[str],
    max_entries: Optional[int],
    json_output: bool,
    summary: bool,
    time: bool,
) -> None:
    """Read RSS feed from URL and display in compact format.

    URL: The URL of the RSS feed to read. If not provided, uses RSS_URL environment variable.
    """
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
