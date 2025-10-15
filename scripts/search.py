#!/usr/bin/env python3
"""
search.py - Multi-source search tool for workspace

Searches across multiple sources and provides token-efficient summaries:
- Tasks, Knowledge, Lessons (always)
- Roam notes (if --roam)
- GitHub issues/PRs (if --github)

Usage:
  ./scripts/search.py <query> [--roam] [--github] [--github-owners owner1,owner2,...] [--verbose]
"""

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple


def git_grep_files(query: str, path_pattern: str | None = None) -> List[Tuple[str, int]]:
    """Search with git grep, return (file, match_count) sorted by relevance."""
    cmd = ["git", "grep", "-l", "-i", query]
    if path_pattern:
        cmd.extend(["--", path_pattern])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path.cwd())
    if result.returncode != 0:
        return []

    # Get match count for each file
    files_with_counts = []
    for file in result.stdout.strip().split("\n"):
        if not file:
            continue
        count_cmd = ["git", "grep", "-c", "-i", query, "--", file]
        count_result = subprocess.run(count_cmd, capture_output=True, text=True, cwd=Path.cwd())
        if count_result.returncode == 0:
            try:
                count = int(count_result.stdout.strip().split(":")[-1])
                files_with_counts.append((file, count))
            except (ValueError, IndexError):
                files_with_counts.append((file, 1))

    # Sort by count (descending)
    return sorted(files_with_counts, key=lambda x: (-x[1], x[0]))


def get_match_snippet(file: str, query: str, context: int = 1) -> str:
    """Get a snippet showing the first match with context lines."""
    cmd = ["git", "grep", "-i", "-n", "-C", str(context), query, "--", file]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path.cwd())
    if result.returncode != 0:
        return ""

    # Take first match group
    lines = result.stdout.strip().split("\n")
    snippet = "\n    ".join(lines[: min(5, len(lines))])
    if len(lines) > 5:
        snippet += "\n    ..."
    return snippet


def search_roam_tasks(query: str) -> List[str]:
    """Search Roam backup for TODO items matching query."""
    roam_backup = Path.home() / "Programming" / "roam-backup" / "json"
    if not roam_backup.exists():
        return []

    results = []
    cmd = ["git", "grep", "-i", "-l", query]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=roam_backup)

    if result.returncode != 0:
        return []

    # Get filenames with matches
    for file in result.stdout.strip().split("\n")[:10]:  # Limit to top 10
        if file:
            results.append(file)

    return results


def search_git_log(query: str, max_results: int = 5) -> List[str]:
    """Search git commit history for query."""
    cmd = [
        "git",
        "log",
        "--all",
        "--grep",
        query,
        "-i",
        "--pretty=format:%h %ad %s",
        "--date=short",
        f"-{max_results}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path.cwd())
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.strip().split("\n") if line]


def search_github(query: str, owners: List[str] | None = None) -> Dict[str, List[str]]:
    """Search GitHub issues and PRs.

    Args:
        query: Search query
        owners: List of GitHub owners/orgs to search. If None, uses default (gptme)
    """
    if owners is None:
        owners = ["gptme"]  # Default to gptme org only

    results: Dict[str, List[str]] = {"issues": [], "prs": []}

    # Build owner flags for gh command
    owner_flags = []
    for owner in owners:
        owner_flags.extend(["--owner", owner])

    # Search issues in relevant repos
    issues_cmd = [
        "gh",
        "search",
        "issues",
        *owner_flags,
        "--sort",
        "updated",
        query,
        "--limit",
        "5",
        "--json",
        "number,title,url",
    ]
    issues_result = subprocess.run(issues_cmd, capture_output=True, text=True)
    if issues_result.returncode == 0 and issues_result.stdout.strip():
        try:
            issues = json.loads(issues_result.stdout)
            results["issues"] = [
                f"#{i['number']}: {i['title'][:60]}... <{i['url']}>"
                if len(i["title"]) > 60
                else f"#{i['number']}: {i['title']} <{i['url']}>"
                for i in issues
            ]
        except json.JSONDecodeError:
            pass

    # Search PRs in relevant repos
    prs_cmd = [
        "gh",
        "search",
        "prs",
        *owner_flags,
        "--sort",
        "updated",
        query,
        "--limit",
        "5",
        "--json",
        "number,title,url",
    ]
    prs_result = subprocess.run(prs_cmd, capture_output=True, text=True)
    if prs_result.returncode == 0 and prs_result.stdout.strip():
        try:
            prs = json.loads(prs_result.stdout)
            results["prs"] = [
                f"#{p['number']}: {p['title'][:60]}... <{p['url']}>"
                if len(p["title"]) > 60
                else f"#{p['number']}: {p['title']} <{p['url']}>"
                for p in prs
            ]
        except json.JSONDecodeError:
            pass

    return results


def format_source_summary(source_name: str, files: List[Tuple[str, int]], query: str, verbose: bool = False) -> str:
    """Format compact summary of results from one source."""
    if not files:
        return f"## {source_name}\nNo matches\n"

    total_matches = sum(count for _, count in files)
    output = [f"## {source_name}\n{len(files)} files, {total_matches} matches"]

    # Top 10 files (compact list)
    output.append("\nTop files:")
    for i, (file, count) in enumerate(files[:10], 1):
        output.append(f"  {i}. {file} ({count})")

    # Top 5 with snippets (if verbose)
    if verbose:
        output.append("\nTop results:")
        for i, (file, count) in enumerate(files[:5], 1):
            snippet = get_match_snippet(file, query)
            if snippet:
                output.append(f"  {i}. {file}:")
                output.append(f"    {snippet}")

    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description="Multi-source workspace search with compact summaries")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--roam", action="store_true", help="Include Roam notes search")
    parser.add_argument("--github", action="store_true", help="Include GitHub issues/PRs search")
    parser.add_argument(
        "--github-owners",
        help="Comma-separated list of GitHub owners/orgs to search (default: gptme). Can also set via GITHUB_SEARCH_OWNERS env var.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show match snippets for top 5 results",
    )

    args = parser.parse_args()

    # Get GitHub owners from args or env var
    github_owners = None
    if args.github:
        if args.github_owners:
            github_owners = [o.strip() for o in args.github_owners.split(",")]
        elif os.environ.get("GITHUB_SEARCH_OWNERS"):
            github_owners = [o.strip() for o in os.environ["GITHUB_SEARCH_OWNERS"].split(",")]

    # Define sources to search
    sources = {
        "Tasks": ["tasks/*.md"],
        "Knowledge": ["knowledge/**/*.md", "knowledge/**/*.txt"],
        "Lessons": ["lessons/**/*.md"],
    }

    # Search each source
    all_results = {}
    for source_name, patterns in sources.items():
        files = [file for pattern in patterns for file in git_grep_files(args.query, pattern)]
        all_results[source_name] = files

    # Print summary header
    total_files = sum(len(files) for files in all_results.values())
    total_matches = sum(sum(count for _, count in files) for files in all_results.values())

    print(f"# Search: '{args.query}'")
    print(f"Total: {total_files} files, {total_matches} matches\n")

    # Print each source (skip if no matches)
    for source_name in ["Tasks", "Knowledge", "Lessons"]:
        files = all_results[source_name]
        if files:  # Only show sources with matches
            print(format_source_summary(source_name, files, args.query, args.verbose))
            print()

    # Git log search
    git_log_results = search_git_log(args.query)
    if git_log_results:
        print("## Git Log")
        print(f"{len(git_log_results)} commits")
        for i, commit in enumerate(git_log_results, 1):
            print(f"  {i}. {commit}")
        print()

    # Optional: Roam tasks
    if args.roam:
        roam_results = search_roam_tasks(args.query)
        print("## Roam Tasks")
        if roam_results:
            print(f"{len(roam_results)} results")
            for i, result in enumerate(roam_results, 1):
                print(f"  {i}. {result}")
        else:
            print("No matches")
        print()

    # Optional: GitHub
    if args.github:
        gh_results = search_github(args.query, github_owners)
        issue_count = len(gh_results["issues"])
        pr_count = len(gh_results["prs"])

        owners_str = ", ".join(github_owners) if github_owners else "gptme (default)"
        print(f"## GitHub (owners: {owners_str})")
        if issue_count or pr_count:
            print(f"{issue_count} issues, {pr_count} PRs")

            if gh_results["issues"]:
                print("\nIssues:")
                for i, issue in enumerate(gh_results["issues"], 1):
                    print(f"  {i}. {issue}")

            if gh_results["prs"]:
                print("\nPull Requests:")
                for i, pr in enumerate(gh_results["prs"], 1):
                    print(f"  {i}. {pr}")
        else:
            print("No matches")
        print()

    # Suggestion for deeper exploration
    if total_files > 0:
        print("💡 Dig deeper:")
        print(f"  git grep -i '{args.query}' -- <file>  # See full matches")
        print("  cat <file>  # Read full content")


if __name__ == "__main__":
    main()
