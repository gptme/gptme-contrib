#!/usr/bin/env python3
"""Fetch community gptme extensions and update community_plugins.json.

Merges two sources:
1. registry.gptme.org/registry.json — curated official list (seed/baseline)
2. GitHub topic search for gptme-plugin, gptme-skill, gptme-mcp-server

The registry provides stable coverage of official gptme/* repos regardless of
whether they have the correct GitHub topics applied. Topic search discovers
third-party community extensions organically. Together they give complete coverage.

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

# Registry repo that maintains the curated official list
REGISTRY_REPO = "gptme/registry.gptme.org"
REGISTRY_FILE = "registry.json"

# Map registry type strings to gptme topic names
_TYPE_TO_TOPICS: dict[str, list[str]] = {
    "plugin": ["gptme-plugin"],
    "skill": ["gptme-skill"],
    "mcp-server": ["gptme-mcp-server"],
    "plugin / skill": ["gptme-plugin", "gptme-skill"],
    "plugin / mcp-server": ["gptme-plugin", "gptme-mcp-server"],
    "skill / mcp-server": ["gptme-skill", "gptme-mcp-server"],
}

DEFAULT_OUTPUT = Path(__file__).parent.parent / "community_plugins.json"


def fetch_registry() -> tuple[list[dict], bool]:
    """Fetch the curated registry from registry.gptme.org and convert to entry format.

    Returns (entries, failed) — failed is True when the registry fetch raised an error.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{REGISTRY_REPO}/contents/{REGISTRY_FILE}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Warning: failed to fetch registry: {exc}", file=sys.stderr)
        return [], True

    import base64

    raw = json.loads(result.stdout)
    registry = json.loads(base64.b64decode(raw["content"]).decode())

    entries = []
    for item in registry:
        name = item.get("name", "")
        if name in EXCLUDE_NAMES:
            continue
        type_str = item.get("type", "")
        topics = _TYPE_TO_TOPICS.get(type_str, [])
        entry = {
            "name": name,
            "description": (item.get("description") or "").strip(),
            "url": item.get("url", f"https://github.com/{name}"),
            "stars": item.get("stars", 0),
            "language": item.get("language") or "",
            "topics": topics,
        }
        entries.append(entry)
    return entries, False


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


def fetch_topics() -> tuple[list[dict], list[str]]:
    """Fetch all repos across all gptme topics, deduplicated by full_name.

    Returns (entries, failed_topics) — failed_topics is non-empty when any
    individual topic search raised an error (partial-failure case).
    """
    seen: dict[str, dict] = {}
    failed_topics: list[str] = []
    for topic in TOPICS:
        try:
            repos = search_topic(topic)
        except subprocess.CalledProcessError as exc:
            print(f"Warning: failed to search topic {topic}: {exc}", file=sys.stderr)
            failed_topics.append(topic)
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
    return entries, failed_topics


def merge_entries(registry: list[dict], topic_hits: list[dict]) -> list[dict]:
    """Merge curated registry entries with topic-search discoveries.

    Registry entries are the baseline. Topic hits supplement with community repos
    not in the curated list, and update live star counts for repos in both.
    Result is sorted descending by stars.
    """
    merged: dict[str, dict] = {e["name"]: e for e in registry}

    for entry in topic_hits:
        name = entry["name"]
        if name in merged:
            # Update live data from GitHub API while preserving registry metadata
            merged[name]["stars"] = entry["stars"]
            merged[name]["language"] = entry["language"] or merged[name]["language"]
            # Prefer the live GitHub html_url over the registry URL (registry may be stale)
            if entry.get("url"):
                merged[name]["url"] = entry["url"]
            # Add any topic tags not already present
            existing = set(merged[name]["topics"])
            merged[name]["topics"] = sorted(existing | set(entry["topics"]))
        else:
            merged[name] = entry

    return sorted(merged.values(), key=lambda e: -(e.get("stars") or 0))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch community gptme extensions from GitHub"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output JSON file (default: community_plugins.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print result without writing"
    )
    args = parser.parse_args()

    print(f"Fetching curated registry from {REGISTRY_REPO}...", file=sys.stderr)
    registry_entries, registry_failed = fetch_registry()
    print(f"Registry: {len(registry_entries)} entries", file=sys.stderr)

    print(f"Searching GitHub for topics: {', '.join(TOPICS)}", file=sys.stderr)
    topic_entries, failed_topics = fetch_topics()
    print(f"Topics: {len(topic_entries)} repos found", file=sys.stderr)

    entries = merge_entries(registry_entries, topic_entries)
    print(f"Merged: {len(entries)} total entries", file=sys.stderr)

    # Refuse to overwrite when any source failed and the previous file has entries —
    # a registry failure silently drops curated repos; a topic failure drops community
    # repos tagged with the failing topic. Either way it's as bad as a total wipe.
    any_source_failed = registry_failed or bool(failed_topics)
    if any_source_failed and not args.dry_run and args.output.exists():
        try:
            prev = json.loads(args.output.read_text())
            if prev.get("entries"):
                failed_labels = (
                    ["registry"] if registry_failed else []
                ) + failed_topics
                print(
                    f"Error: fetch failed for: {', '.join(failed_labels)}."
                    " Refusing to overwrite previous data — check GitHub API access.",
                    file=sys.stderr,
                )
                return 1
        except (json.JSONDecodeError, KeyError):
            pass

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"registry.gptme.org + github-topics: {', '.join(TOPICS)}",
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
