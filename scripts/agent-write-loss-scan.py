#!/usr/bin/env python3
"""Detect write-loss in gptme agent sessions (git-clobber hazard).

An agent writes to a file inside a git-tracked workspace via the ``save`` or
``append`` tool, but the change is later silently reverted -- the file goes
back to its git-HEAD state and the write is lost. This scanner measures how
often that happens.

Classification of each detected write:

  PERSISTED   -- the content was committed to git (at or after write time)
  SUPERSEDED  -- a later commit changed the file to *different* content (not a
                 loss: the write was incorporated or intentionally replaced)
  LOST        -- the path reverted to its pre-write state; content was never
                 committed
  UNKNOWN     -- git history is ambiguous (untracked path, edit heuristic
                 inconclusive, etc.)

Usage::

    agent-write-loss-scan --repo /path/to/workspace
    agent-write-loss-scan --repo /path/to/workspace --json
    agent-write-loss-scan --repo /path/to/workspace --since 2025-01-01
    agent-write-loss-scan --repo /path/to/workspace --logs-dir ~/.local/share/gptme/logs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LOGS_DIR = Path.home() / ".local" / "share" / "gptme" / "logs"

# Matches gptme's fence-tool syntax:  ```save path/to/file\ncontent\n```
_FENCE_SAVE_RE = re.compile(r"```(save|append)\s+(\S+)\n(.*?)\n```", re.DOTALL)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WriteEvent:
    session_id: str
    tool: str  # "save" | "append"
    rel_path: str  # path relative to repo root
    write_ts: float
    written_blob: str | None = None  # git blob SHA for full saves
    new_strings: list[str] = field(default_factory=list)  # substrings for appends
    outcome: str = "UNKNOWN"  # PERSISTED | SUPERSEDED | LOST | UNKNOWN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_blob_sha(data: bytes) -> str:
    """Compute a SHA-1 Git blob ID (kept as a public testable helper)."""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def _hash_blob(repo_root: Path, data: bytes) -> str | None:
    """Ask Git to hash content using this repository's object format."""
    try:
        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            cwd=repo_root,
            input=data,
            capture_output=True,
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip().decode() if result.returncode == 0 else None


def _parse_iso_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    s = raw.strip().rstrip("Z") + "+00:00" if raw.strip().endswith("Z") else raw.strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _run_git(repo_root: Path, args: list[str], timeout: float = 20.0) -> str | None:
    """Run a read-only git command; return stdout or None on error."""
    # Safety: refuse any command that could mutate the working tree.
    _MUTATING = {
        "checkout",
        "reset",
        "clean",
        "stash",
        "restore",
        "revert",
        "merge",
        "rebase",
        "cherry-pick",
        "apply",
        "am",
        "update-ref",
    }
    subcmd = next((a for a in args if not a.startswith("-")), "")
    if subcmd in _MUTATING:
        raise RuntimeError(f"refusing mutating git op: git {subcmd}")
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _repo_root_for(abs_path: str, repo_root: Path) -> str | None:
    """Return path relative to repo_root, or None if outside it."""
    try:
        rel = Path(abs_path).relative_to(repo_root)
        return str(rel)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsing gptme conversations
# ---------------------------------------------------------------------------


def parse_conversation_jsonl(
    conv_path: Path, repo_root: Path
) -> tuple[float, list[WriteEvent]]:
    """Parse a gptme conversation.jsonl and return (session_start_ts, write_events)."""
    start_ts: float | None = None
    events: list[WriteEvent] = []

    try:
        lines = conv_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0, []

    session_id = conv_path.parent.name

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = _parse_iso_ts(obj.get("timestamp"))
        if ts is not None:
            start_ts = ts if start_ts is None else min(start_ts, ts)

        if obj.get("role") != "assistant":
            continue
        content = obj.get("content")
        if not isinstance(content, str):
            continue

        for m in _FENCE_SAVE_RE.finditer(content):
            tool, raw_path, body = m.group(1), m.group(2), m.group(3)
            if raw_path.startswith("/"):
                abs_path = raw_path
            else:
                abs_path = str(repo_root / raw_path)
            rel = _repo_root_for(abs_path, repo_root)
            if rel is None:
                continue
            ev = WriteEvent(
                session_id=session_id,
                tool=tool,
                rel_path=rel,
                write_ts=ts or (start_ts or 0.0),
            )
            if tool == "save":
                content_bytes = (body + "\n").encode("utf-8", "replace")
                ev.written_blob = _hash_blob(repo_root, content_bytes)
                ev.new_strings.append(body)
            else:
                ev.new_strings.append(body)
            events.append(ev)

    return start_ts or 0.0, events


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _git_since(write_ts: float) -> str:
    """Format a Unix timestamp for ``git log --since``.

    Do NOT use git's ``@<seconds>`` form here: git parses it with approxidate,
    which rejects values too small to look like a real date (anything below
    roughly 1e8) and then silently falls back to *now*. A bogus cutoff of "now"
    filters out every commit, so writes carrying a missing or zero timestamp
    would all be misreported as LOST. An explicit ISO-8601 instant parses
    correctly across the whole range.
    """
    return datetime.fromtimestamp(max(0, int(write_ts)), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z"
    )


def _post_write_blobs(
    repo_root: Path, rel_path: str, write_ts: float
) -> list[tuple[str, int]]:
    """Return full blob IDs from commits whose committer time follows the write."""
    out = _run_git(
        repo_root,
        [
            "log",
            "--format=C|%ct",
            "--raw",
            "--no-abbrev",
            f"--since={_git_since(write_ts)}",
            "--",
            rel_path,
        ],
    )
    if not out:
        return []
    blobs: list[tuple[str, int]] = []
    cur_time = 0
    for line in out.splitlines():
        if line.startswith("C|"):
            try:
                cur_time = int(line.removeprefix("C|"))
            except ValueError:
                cur_time = 0
        elif line.startswith(":") and cur_time >= write_ts:
            fields = line.split("\t", 1)[0].split()
            if len(fields) >= 4:
                blobs.append((fields[3], cur_time))
    return blobs


def classify_write(ev: WriteEvent, repo_root: Path) -> WriteEvent:
    """Classify a single write event against git history. Mutates and returns ev."""
    blobs = _post_write_blobs(repo_root, ev.rel_path, ev.write_ts)

    if ev.written_blob:
        # Full-content save: only an exact blob in a post-write commit persists it.
        for blob, _t in blobs:
            if blob == ev.written_blob:
                ev.outcome = "PERSISTED"
                return ev
        if blobs:
            ev.outcome = "SUPERSEDED"
        else:
            ev.outcome = "LOST"
        return ev

    # Append: check if content substring appears in post-write commits
    if not ev.new_strings:
        ev.outcome = "UNKNOWN"
        return ev

    if not blobs:
        ev.outcome = "LOST"
        return ev

    # Check the most recent post-write committed blob for substring presence
    for blob, _t in blobs[:5]:
        blob_text = _run_git(repo_root, ["cat-file", "blob", blob])
        if blob_text and any(s in blob_text for s in ev.new_strings):
            ev.outcome = "PERSISTED"
            return ev

    ev.outcome = "SUPERSEDED"
    return ev


# ---------------------------------------------------------------------------
# Session scanning
# ---------------------------------------------------------------------------


def scan_logs(
    logs_dir: Path,
    repo_root: Path,
    since: float | None = None,
    limit: int | None = None,
) -> list[WriteEvent]:
    """Scan all gptme sessions in logs_dir and classify their write events."""
    convs: list[Path] = []
    if logs_dir.is_dir():
        for entry in sorted(logs_dir.iterdir(), reverse=True):
            conv = entry / "conversation.jsonl"
            if conv.is_file():
                convs.append(conv)
                if limit and len(convs) >= limit:
                    break

    all_events: list[WriteEvent] = []
    for conv in convs:
        sess_ts, events = parse_conversation_jsonl(conv, repo_root)
        if since and sess_ts and sess_ts < since:
            continue
        for ev in events:
            if since and ev.write_ts < since:
                continue
            classify_write(ev, repo_root)
            all_events.append(ev)

    return all_events


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _summarise(events: list[WriteEvent]) -> dict:
    from collections import Counter

    counts: Counter[str] = Counter(ev.outcome for ev in events)
    total = len(events)
    lost = counts["LOST"]
    persisted = counts["PERSISTED"]
    return {
        "total_writes": total,
        "persisted": persisted,
        "superseded": counts["SUPERSEDED"],
        "lost": lost,
        "unknown": counts["UNKNOWN"],
        "loss_rate": round(lost / total, 4) if total else 0.0,
        "loss_pct": f"{100.0 * lost / total:.1f}%" if total else "0.0%",
        "events": [
            {
                "session_id": ev.session_id,
                "tool": ev.tool,
                "rel_path": ev.rel_path,
                "outcome": ev.outcome,
            }
            for ev in events
        ],
    }


def _print_text(summary: dict) -> None:
    sep = "=" * 60
    print(sep)
    print("agent-write-loss-scan")
    print(sep)
    print(f"total writes scanned : {summary['total_writes']}")
    print(f"persisted            : {summary['persisted']}")
    print(f"superseded           : {summary['superseded']}")
    print(f"lost                 : {summary['lost']}")
    print(f"unknown              : {summary['unknown']}")
    print(f"loss rate            : {summary['loss_pct']}")
    if summary["lost"] > 0:
        print()
        print("Lost writes:")
        for ev in summary["events"]:
            if ev["outcome"] == "LOST":
                print(f"  [{ev['session_id']}] {ev['tool']} → {ev['rel_path']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--repo",
        required=True,
        metavar="PATH",
        help="Path to the git repository being monitored.",
    )
    ap.add_argument(
        "--logs-dir",
        metavar="PATH",
        default=str(DEFAULT_LOGS_DIR),
        help=f"gptme logs directory (default: {DEFAULT_LOGS_DIR}).",
    )
    ap.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only scan sessions on or after this date.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Cap number of sessions to scan (newest first).",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON.",
    )
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    if not (repo_root / ".git").exists():
        print(f"error: {repo_root} is not a git repository", file=sys.stderr)
        return 1

    logs_dir = Path(args.logs_dir)
    since: float | None = None
    if args.since:
        try:
            since = (
                datetime.strptime(args.since, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            print(
                f"error: --since must be YYYY-MM-DD, got {args.since!r}",
                file=sys.stderr,
            )
            return 1

    events = scan_logs(logs_dir, repo_root, since=since, limit=args.limit)
    summary = _summarise(events)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_text(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
