#!/usr/bin/env python3
"""Fetch GitHub trending repositories in compact LLM-friendly format.

Scrapes github.com/trending (no API auth required).
Designed for agent social consumption sessions.

Usage:
    ./scripts/fetch-github-trending.py                    # All languages
    ./scripts/fetch-github-trending.py --lang python      # Python only
    ./scripts/fetch-github-trending.py --lang rust --since weekly
    ./scripts/fetch-github-trending.py --filter "agent,llm,cli"
"""

import argparse
import json
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

TRENDING_URL = "https://github.com/trending"
TIMEOUT = 15


def fetch_html(url: str) -> str | None:
    """Fetch HTML from URL."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; gptme/1.0)",
                "Accept": "text/html",
            },
        )
        with urlopen(req, timeout=TIMEOUT) as resp:
            data: bytes = resp.read()
            return data.decode("utf-8", errors="replace")
    except (URLError, TimeoutError):
        return None


def parse_trending(html: str) -> list[dict]:
    """Parse trending repos from HTML.

    Extracts repo name, description, language, stars, and today's stars
    using regex patterns (avoids heavy BeautifulSoup dependency).
    """
    repos = []

    # Each repo is in an <article> element with class "Box-row"
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL)

    for article in articles:
        # Repo name: /owner/name in an <h2> link
        name_match = re.search(
            r'<h2[^>]*>.*?<a[^>]*href="(/[^"]+)"', article, re.DOTALL
        )
        if not name_match:
            continue
        full_name = name_match.group(1).strip("/")

        # Description
        desc_match = re.search(
            r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', article, re.DOTALL
        )
        description = ""
        if desc_match:
            description = re.sub(r"<[^>]+>", "", desc_match.group(1)).strip()

        # Language
        lang_match = re.search(
            r'<span[^>]*itemprop="programmingLanguage"[^>]*>(.*?)</span>',
            article,
            re.DOTALL,
        )
        language = lang_match.group(1).strip() if lang_match else ""

        # Total stars (in the stargazers link)
        stars_match = re.search(
            r'href="/[^"]+/stargazers"[^>]*>\s*([\d,]+)\s*</a>',
            article,
            re.DOTALL,
        )
        stars = 0
        if stars_match:
            stars = int(stars_match.group(1).replace(",", ""))

        # Today's stars
        today_match = re.search(r"([\d,]+)\s+stars?\s+today", article, re.DOTALL)
        today_stars = 0
        if today_match:
            today_stars = int(today_match.group(1).replace(",", ""))

        repos.append(
            {
                "name": full_name,
                "description": description,
                "language": language,
                "stars": stars,
                "today_stars": today_stars,
                "url": f"https://github.com/{full_name}",
            }
        )

    return repos


def format_compact(repos: list[dict], keywords: list[str] | None = None) -> str:
    """Format repos in compact format.

    Output:
        [★stars +today] owner/name (Language) — Description
        URL: <url>
    """
    lines = []
    for r in repos:
        if keywords:
            text = f"{r['name']} {r['description']} {r['language']}".lower()
            if not any(kw.lower() in text for kw in keywords):
                continue

        lang = f" ({r['language']})" if r["language"] else ""
        today = f" +{r['today_stars']}" if r["today_stars"] else ""
        desc = f" — {r['description']}" if r["description"] else ""
        lines.append(f"[★{r['stars']:>6}{today}] {r['name']}{lang}{desc}")
        lines.append(f"          {r['url']}")

    if not lines:
        return "No repositories matched the filter criteria."

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GitHub trending repos")
    parser.add_argument(
        "--lang",
        type=str,
        default="",
        help="Programming language filter (e.g. python, rust, typescript)",
    )
    parser.add_argument(
        "--since",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="Time range (default: daily)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--filter",
        type=str,
        help="Comma-separated keywords to filter by (case-insensitive)",
    )
    args = parser.parse_args()

    url = TRENDING_URL
    if args.lang:
        url += f"/{args.lang.lower()}"
    url += f"?since={args.since}"

    html = fetch_html(url)
    if not html:
        print("Error: Could not fetch GitHub trending page", file=sys.stderr)
        sys.exit(1)

    repos = parse_trending(html)
    if not repos:
        print(
            "Warning: No repos parsed (page format may have changed)", file=sys.stderr
        )
        sys.exit(0)

    keywords = [k.strip() for k in args.filter.split(",")] if args.filter else None

    if args.json:
        filtered = repos
        if keywords:
            filtered = [
                r
                for r in repos
                if any(
                    kw.lower()
                    in f"{r['name']} {r['description']} {r['language']}".lower()
                    for kw in keywords
                )
            ]
        json.dump(filtered, sys.stdout, indent=2)
        print()
    else:
        lang_label = f" ({args.lang})" if args.lang else ""
        print(f"# GitHub Trending{lang_label} — {args.since}")
        print()
        print(format_compact(repos, keywords))


if __name__ == "__main__":
    main()
