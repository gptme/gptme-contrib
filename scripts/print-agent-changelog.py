#!/usr/bin/env python3
"""Print agent-facing gptme-contrib changelog entries between two pins.

Reads CHANGELOG.md at OLD and NEW and emits entries that are new or edited in
NEW: a change to the date, title, Action or Do text counts. Code-only pin bumps
produce no output.

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
ANY_HEADING = re.compile(r"^(?:#{1,6}\s|##)")
FIELD = re.compile(r"^\s*-\s+\*\*(Action|Do):\*\*\s+(.+?)\s*$", re.IGNORECASE)
FENCE = re.compile(r"^(`{3,}|~{3,})")


@dataclass(frozen=True)
class Entry:
    date: str
    title: str
    action: str
    do: str


def normalize_action(raw: str) -> str:
    """First token of an Action line, with wrapping backticks stripped."""
    token = raw.strip().strip("`")
    if not token:
        return ""
    return token.split()[0].strip("`").lower()


def parse_entries(text: str, *, strict: bool = True) -> list[Entry]:
    """Parse dated changelog entries; ignore prose and fenced examples.

    An entry runs from its "## date - title" heading to the next heading, and
    everything from the Do field to that point is its instruction.

    In *strict* mode (NEW), anything that could hide or alter an instruction
    raises RuntimeError: a missing, repeated or unknown field, a field outside a
    dated entry (usually a mistyped heading), a code fence that is unclosed, or
    one that contains a dated heading or an entry's field. Silent drops look
    like a code-only bump to callers.

    In non-strict mode (OLD) those rows are skipped so a typo that was later
    fixed does not permanently fail every range that starts at that pin.
    """
    entries: list[Entry] = list()
    current: dict[str, str] | None = None
    fence = ""

    def fail(message: str) -> None:
        if strict:
            raise RuntimeError(message)

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        row, current = current, None
        label = f"{row['date']} - {row['title']}"
        action = row.get("action")
        if "error" in row:
            fail(f"{label}: {row['error']}")
        elif action is None:
            fail(f"{label}: missing Action")
        elif action not in RANK or action == "none":
            fail(f"{label}: unparseable Action {action!r}")
        elif "do" not in row:
            fail(f"{label}: missing Do")
        else:
            entries.append(Entry(row["date"], row["title"], action, row["do"]))

    for line in text.splitlines():
        if fence:
            closing = line.strip()
            if closing.startswith(fence) and not closing.strip(fence[0]):
                fence = ""
            elif HEADING.match(line) or (current and FIELD.match(line)):
                # A later fence would close this one and hide the rows between.
                fail(f"entry line inside a code fence (unclosed?): {line!r}")
        elif opening := FENCE.match(line):
            fence = opening.group(1)
        elif ANY_HEADING.match(line):
            flush()
            if match := HEADING.match(line):
                current = {"date": match.group(1), "title": match.group(2).strip()}
            continue
        elif field := FIELD.match(line):
            name, value = field.group(1).lower(), field.group(2)
            if current is None:
                fail(f"field outside a dated entry (check the heading): {line!r}")
            elif name in current:
                current["error"] = f"repeated {field.group(1)}"
            else:
                current[name] = normalize_action(value) if name == "action" else value
            continue
        if current and "do" in current and line.strip():
            current["do"] += " " + line.strip()
    flush()
    if fence:
        fail("unclosed code fence")
    return entries


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


def git_show(repo: Path, rev: str, path: str, *, missing_ok: bool = False) -> str:
    """Return *path* at *rev*.

    Empty output means "nothing to adopt" to callers, so every failed lookup
    raises. The one exception, with ``missing_ok``, is a valid revision that
    does not contain the file.
    """
    # cat-file blob, not show: show lists a directory and exits 0.
    result = run_git(repo, "cat-file", "blob", f"{rev}:{path}")
    if result.returncode == 0:
        return result.stdout
    if missing_ok:
        listed = run_git(repo, "ls-tree", "--name-only", rev, "--", path)
        if listed.returncode == 0 and not listed.stdout.strip():
            return ""
    raise RuntimeError(result.stderr.strip() or f"cannot read {rev}:{path}")


def entries_between(
    repo: Path, old: str, new: str, *, path: str = CHANGELOG
) -> list[Entry]:
    new_entries = parse_entries(git_show(repo, new, path))
    old_text = git_show(repo, old, path, missing_ok=True)
    seen = set(parse_entries(old_text, strict=False))
    return [entry for entry in new_entries if entry not in seen]


def filter_action(entries: list[Entry], min_action: str) -> list[Entry]:
    floor = RANK[min_action]
    return [entry for entry in entries if RANK[entry.action] >= floor]


def render_text(entries: list[Entry]) -> str:
    lines: list[str] = list()
    for entry in entries:
        lines.append(f"{entry.action}  {entry.date}  {entry.title}")
        lines.append(f"  {entry.do}")
    return "\n".join(lines)


def script_checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_repo(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    return script_checkout()


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
        help="gptme-contrib checkout (default: this script's repository)",
    )
    parser.add_argument(
        "--path",
        default=CHANGELOG,
        help="changelog path relative to the repo root",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    floor = parser.add_mutually_exclusive_group()
    floor.add_argument(
        "--min-action",
        choices=ACTIONS[1:],
        default="recommended",
        help="lowest action class to print (default: recommended)",
    )
    floor.add_argument(
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
    except (
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
        UnicodeDecodeError,
    ) as exc:
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
