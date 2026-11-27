#!/usr/bin/env python3
"""Print agent-facing gptme-contrib changelog entries between two pins.

Reads CHANGELOG.md at OLD and NEW and emits entries whose (date, title) pair
appeared in NEW but not OLD. Code-only pin bumps produce no output.

Usage::

    python3 scripts/print-agent-changelog.py OLD NEW
    python3 scripts/print-agent-changelog.py OLD NEW --json
    python3 scripts/print-agent-changelog.py OLD NEW --all
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
ACTIONS = ("none", "optional", "recommended", "required")
RANK = {name: i for i, name in enumerate(ACTIONS)}
HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s+[—–-]\s+(.+)$")
ACTION = re.compile(r"^\s*-\s+\*\*Action:\*\*\s+(\w+)\s*$", re.IGNORECASE)
DO = re.compile(r"^\s*-\s+\*\*Do:\*\*\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Entry:
    date: str
    title: str
    action: str
    do: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.date, self.title)


def parse_entries(text: str) -> list[Entry]:
    """Parse dated changelog entries; ignore prose and the format example."""
    entries: list[Entry] = list()
    current: dict[str, str] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        action = current.get("action", "")
        body = current.get("do", "").strip()
        if action in RANK and action != "none" and body:
            entries.append(Entry(current["date"], current["title"], action, body))
        current = None

    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            current = {"date": match.group(1), "title": match.group(2).strip()}
            continue
        if current is None:
            continue
        action_match = ACTION.match(line)
        if action_match:
            current["action"] = action_match.group(1).lower()
            continue
        do_match = DO.match(line)
        if do_match:
            current["do"] = do_match.group(1).strip()
            continue
        if "do" in current and line.startswith(" ") and line.strip():
            current["do"] += " " + line.strip()
    flush()
    return entries


def git_show(repo: Path, rev: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        return ""
    return result.stdout


def entries_between(
    repo: Path, old: str, new: str, *, path: str = CHANGELOG
) -> list[Entry]:
    new_entries = parse_entries(git_show(repo, new, path))
    old_keys = {entry.key for entry in parse_entries(git_show(repo, old, path))}
    return [entry for entry in new_entries if entry.key not in old_keys]


def filter_action(entries: list[Entry], min_action: str) -> list[Entry]:
    floor = RANK[min_action]
    return [entry for entry in entries if RANK[entry.action] >= floor]


def render_text(entries: list[Entry]) -> str:
    lines: list[str] = list()
    for entry in entries:
        lines.append(f"{entry.action}  {entry.date}  {entry.title}")
        lines.append(f"  {entry.do}")
    return "\n".join(lines)


def resolve_repo(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("not inside a git repository; pass --repo")
    return Path(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("old", help="previous pin (SHA or ref)")
    parser.add_argument("new", help="new pin (SHA or ref)")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="gptme-contrib checkout (default: current git root)",
    )
    parser.add_argument(
        "--path",
        default=CHANGELOG,
        help="changelog path relative to the repo root",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--min-action",
        choices=ACTIONS[1:],
        default="recommended",
        help="lowest action class to print (default: recommended)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include optional entries (same as --min-action optional)",
    )
    args = parser.parse_args(argv)
    min_action = "optional" if args.all else args.min_action
    try:
        repo = resolve_repo(args.repo)
        entries = filter_action(
            entries_between(repo, args.old, args.new, path=args.path),
            min_action,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([asdict(entry) for entry in entries]))
    else:
        text = render_text(entries)
        if text:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
