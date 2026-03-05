"""CLI entry point for gptme-sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .record import SessionRecord
from .store import (
    SessionStore,
    compute_run_analytics,
    format_run_analytics,
    format_stats,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Session tracking and analytics for gptme agents",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=None,
        help="Path to sessions directory (default: ./state/sessions)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # Query (default)
    query_parser = subparsers.add_parser("query", help="Query session records")
    query_parser.add_argument("--model", help="Filter by model (e.g. opus, sonnet)")
    query_parser.add_argument("--run-type", help="Filter by run type")
    query_parser.add_argument("--category", help="Filter by category")
    query_parser.add_argument("--harness", help="Filter by harness")
    query_parser.add_argument("--outcome", help="Filter by outcome")
    query_parser.add_argument("--since", help="Filter by recency (e.g. 7d, 30d)")
    query_parser.add_argument("--json", action="store_true", help="Output as JSON")
    query_parser.add_argument("--stats", action="store_true", help="Show summary statistics")

    # Append
    append_parser = subparsers.add_parser("append", help="Append a session record")
    append_parser.add_argument("--harness", default="claude-code", help="Harness name")
    append_parser.add_argument("--model", default="unknown", help="Model name")
    append_parser.add_argument("--run-type", default="autonomous", help="Run type")
    append_parser.add_argument("--category", help="Category (e.g. code, content)")
    append_parser.add_argument("--outcome", default="unknown", help="Session outcome")
    append_parser.add_argument("--duration", type=int, default=0, help="Duration in seconds")
    append_parser.add_argument("--selector-mode", help="Selector mode used")
    append_parser.add_argument("--journal-path", help="Path to journal entry")
    append_parser.add_argument("--deliverables", nargs="*", default=[], help="Commit SHAs, PR URLs")
    append_parser.add_argument(
        "--tokens", type=int, default=None, help="Total input+output token count for the session"
    )
    append_parser.add_argument("--cost-usd", type=float, default=None, help="Total cost in USD")

    # Stats shortcut
    stats_parser = subparsers.add_parser("stats", help="Show summary statistics")
    stats_parser.add_argument("--model", help="Filter by model")
    stats_parser.add_argument("--run-type", help="Filter by run type")
    stats_parser.add_argument("--category", help="Filter by category")
    stats_parser.add_argument("--harness", help="Filter by harness")
    stats_parser.add_argument("--outcome", help="Filter by outcome")
    stats_parser.add_argument("--since", help="Filter by recency (e.g. 7d, 30d)")
    stats_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # Runs analytics
    runs_parser = subparsers.add_parser("runs", help="Run analytics (duration, NOOP rate, trends)")
    runs_parser.add_argument("--since", default="14d", help="Time window (e.g. 7d, 30d)")
    runs_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    store = SessionStore(sessions_dir=args.sessions_dir)

    if args.command == "append":
        record = SessionRecord(
            harness=args.harness,
            model=args.model,
            run_type=args.run_type,
            category=args.category,
            outcome=args.outcome,
            duration_seconds=args.duration,
            selector_mode=args.selector_mode,
            journal_path=args.journal_path,
            deliverables=args.deliverables or [],
            token_count=args.tokens,
            cost_usd=args.cost_usd,
        )
        path = store.append(record)
        print(f"Appended session {record.session_id} to {path}")
        return 0

    # Parse --since into days
    since_days = None
    since_val = getattr(args, "since", None)
    if since_val:
        try:
            if since_val.endswith("d"):
                since_days = int(since_val[:-1])
            else:
                since_days = int(since_val)
        except ValueError:
            print(
                f"error: invalid --since value {since_val!r} (expected e.g. 7d, 30d)",
                file=sys.stderr,
            )
            return 1

    if args.command == "stats" or (args.command == "query" and getattr(args, "stats", False)):
        records = store.query(
            model=getattr(args, "model", None),
            run_type=getattr(args, "run_type", None),
            category=getattr(args, "category", None),
            harness=getattr(args, "harness", None),
            outcome=getattr(args, "outcome", None),
            since_days=since_days,
        )
        s = store.stats(records)
        if getattr(args, "json", False):
            print(json.dumps(s, indent=2))
        else:
            format_stats(s)
        return 0

    if args.command == "query":
        records = store.query(
            model=args.model,
            run_type=args.run_type,
            category=args.category,
            harness=args.harness,
            outcome=args.outcome,
            since_days=since_days,
        )
        if args.json:
            print(json.dumps([r.to_dict() for r in records], indent=2))
        else:
            for r in records:
                status = "+" if r.outcome == "productive" else "-"
                cat = r.category or "?"
                dur = f"{r.duration_seconds // 60:3d}m" if r.duration_seconds > 0 else "   ?"
                print(
                    f"[{status}] {r.timestamp[:16]}  {(r.model_normalized or 'unknown'):8s}  "
                    f"{(r.run_type or 'unknown'):12s}  {cat:14s}  {dur}  {r.outcome}"
                )
            print(f"\n{len(records)} records")
        return 0

    if args.command == "runs":
        records = store.query(since_days=since_days)
        analytics = compute_run_analytics(records)
        if args.json:
            print(json.dumps(analytics, indent=2))
        else:
            format_run_analytics(analytics)
        return 0

    # No command — show stats by default
    s = store.stats()
    format_stats(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
