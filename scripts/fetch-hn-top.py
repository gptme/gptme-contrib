#!/usr/bin/env python3
"""Fetch top Hacker News stories in compact LLM-friendly format.

Uses the official HN Firebase API (no auth required).
Designed for agent news consumption sessions.

Usage:
    ./scripts/fetch-hn-top.py              # Top 15 stories
    ./scripts/fetch-hn-top.py --limit 30   # More stories
    ./scripts/fetch-hn-top.py --json       # JSON output for piping
    ./scripts/fetch-hn-top.py --filter "agent,llm,cli"  # Keyword filter
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

HN_API = "https://hacker-news.firebaseio.com/v0"
DEFAULT_LIMIT = 15
TIMEOUT = 10


def fetch_json(url: str) -> Any:
    """Fetch JSON from URL. Returns parsed JSON or None on error."""
    try:
        req = Request(url, headers={"User-Agent": "gptme/1.0"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except (URLError, json.JSONDecodeError, TimeoutError):
        return None


def fetch_item(item_id: int) -> dict[str, Any] | None:
    """Fetch a single HN item."""
    result = fetch_json(f"{HN_API}/item/{item_id}.json")
    return result if isinstance(result, dict) else None


def fetch_top_stories(limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Fetch top stories with parallel item fetching."""
    story_ids = fetch_json(f"{HN_API}/topstories.json")
    if not story_ids:
        print("Error: Could not fetch HN top stories", file=sys.stderr)
        return []

    story_ids = story_ids[:limit]
    stories = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_item, sid): sid for sid in story_ids}
        for future in as_completed(futures):
            item = future.result()
            if item and item.get("type") == "story":
                stories.append(item)

    # Sort by score descending
    stories.sort(key=lambda s: s.get("score", 0), reverse=True)
    return stories


def format_compact(stories: list[dict], keywords: list[str] | None = None) -> str:
    """Format stories in compact one-line format.

    Output format:
        [score] Title — domain (comments_count comments)
        URL: <url>
    """
    lines = []
    for s in stories:
        title = s.get("title", "")
        url = s.get("url", f"https://news.ycombinator.com/item?id={s['id']}")
        score = s.get("score", 0)
        comments = s.get("descendants", 0)

        # Apply keyword filter if specified
        if keywords:
            text = f"{title} {url}".lower()
            if not any(kw.lower() in text for kw in keywords):
                continue

        # Extract domain
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = ""

        domain_str = f" — {domain}" if domain else ""
        lines.append(f"[{score:>4}] {title}{domain_str} ({comments} comments)")
        lines.append(f"       {url}")

    if not lines:
        return "No stories matched the filter criteria."

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch HN top stories")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of stories (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--filter",
        type=str,
        help="Comma-separated keywords to filter by (case-insensitive)",
    )
    args = parser.parse_args()

    keywords = [k.strip() for k in args.filter.split(",")] if args.filter else None

    stories = fetch_top_stories(args.limit)

    if args.json:
        filtered = stories
        if keywords:
            filtered = [
                s
                for s in stories
                if any(
                    kw.lower() in f"{s.get('title', '')} {s.get('url', '')}".lower()
                    for kw in keywords
                )
            ]
        json.dump(
            [
                {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "url": s.get("url", ""),
                    "score": s.get("score", 0),
                    "comments": s.get("descendants", 0),
                    "hn_url": f"https://news.ycombinator.com/item?id={s['id']}",
                }
                for s in filtered
            ],
            sys.stdout,
            indent=2,
        )
        print()
    else:
        print(f"# Hacker News Top {args.limit} (by score)")
        print()
        print(format_compact(stories, keywords))


if __name__ == "__main__":
    main()
