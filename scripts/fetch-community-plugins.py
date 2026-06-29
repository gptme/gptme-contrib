#!/usr/bin/env python3
"""Fetch community gptme extensions from GitHub topics and update state/community_plugins.json.

Searches GitHub for repos tagged with gptme-plugin, gptme-skill, or gptme-mcp-server
topics and writes the results to state/community_plugins.json for use by gptme-dashboard.

Usage:
    python3 scripts/fetch-community-plugins.py
    python3 scripts/fetch-community-plugins.py --output path/to/output.json
    python3 scripts/fetch-community-plugins.py --dry-run

Requires GITHUB_TOKEN env var (or gh CLI auth) for the GitHub search API.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

TOPICS = ["gptme-plugin", "gptme-skill", "gptme-mcp-server"]

# Repos to exclude (the contrib repo itself, which is the host)
EXCLUDE_NAMES = {"gptme/gptme-contrib"}

DEFAULT_OUTPUT = Path(__file__).parent.parent / "state" / "community_plugins.json"


def search_topic(topic: str) -> list[dict]:
    """Search GitHub for repos with the given topic."""
    result = subprocess.run(
        ["gh", "api", f"search/repositories?q=topic:{topic}&per_page=100&sort=stars"],
        capture_output=True,
        text=True,
        check=True,
    )
    data: dict = json.loads(result.stdout)
    items = cast(list[dict], data.get("items", []))
    if len(items) >= 100:
        print(
            f"Warning: topic {topic!r} returned 100 results — may be truncated;"
            " consider adding pagination if the ecosystem grows.",
            file=sys.stderr,
        )
    return items


def fetch_all() -> list[dict]:
    """Fetch all repos across all gptme topics, deduplicated by full_name."""
    seen: dict[str, dict] = {}
    for topic in TOPICS:
        try:
            repos = search_topic(topic)
        except subprocess.CalledProcessError as exc:
            print(f"Warning: failed to search topic {topic}: {exc}", file=sys.stderr)
            continue
        for repo in repos:
            name = repo["full_name"]
            if name in EXCLUDE_NAMES:
                continue
            if name not in seen:
                seen[name] = repo
            # else: merged topics from duplicate hits tracked by TOPICS

    entries = []
    for name, repo in sorted(
        seen.items(), key=lambda x: -(x[1].get("stargazers_count") or 0)
    ):
        topics = [t for t in repo.get("topics", []) if t in TOPICS]
        entry = {
            "name": name,
            "description": (repo.get("description") or "").strip(),
            "url": repo["html_url"],
            "stars": repo.get("stargazers_count", 0),
            "language": repo.get("language") or "",
            "topics": topics,
        }
        entries.append(entry)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch community gptme extensions from GitHub"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output JSON file (default: state/community_plugins.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print result without writing"
    )
    args = parser.parse_args()

    print(f"Searching GitHub for topics: {', '.join(TOPICS)}", file=sys.stderr)
    entries = fetch_all()
    print(f"Found {len(entries)} community repos", file=sys.stderr)

    # Refuse to overwrite a non-empty file with an empty result set — all topic
    # searches failing (rate-limit, auth expiry, outage) must not silently wipe
    # the dashboard data.
    if not entries and not args.dry_run and args.output.exists():
        try:
            prev = json.loads(args.output.read_text())
            if prev.get("entries"):
                print(
                    "Error: all searches returned 0 results but previous file has entries."
                    " Refusing to overwrite — check GitHub API access.",
                    file=sys.stderr,
                )
                return 1
        except (json.JSONDecodeError, KeyError):
            pass

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "github-topics: " + ", ".join(TOPICS),
        "entries": entries,
    }

    json_str = json.dumps(output, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(json_str)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_str + "\n")
    print(f"Written {len(entries)} entries to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
