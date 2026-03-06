"""CLI entry point for gptme-sessions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .discovery import (
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
)
from .post_session import post_session
from .record import SessionRecord
from .signals import extract_from_path
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
    append_parser.add_argument(
        "--category", help="Work category inferred or actual (e.g. code, infrastructure)"
    )
    append_parser.add_argument(
        "--recommended-category", help="Category recommended by selector (e.g. Thompson sampling)"
    )
    append_parser.add_argument("--outcome", default="unknown", help="Session outcome")
    append_parser.add_argument("--duration", type=int, default=0, help="Duration in seconds")
    append_parser.add_argument("--selector-mode", help="Selector mode used")
    append_parser.add_argument("--journal-path", help="Path to journal entry")
    append_parser.add_argument("--deliverables", nargs="*", default=[], help="Commit SHAs, PR URLs")

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

    # Discover — find trajectory files from all harnesses
    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover trajectory files from gptme, Claude Code, Codex, and Copilot harnesses",
    )
    discover_parser.add_argument(
        "--harness",
        choices=["gptme", "claude-code", "codex", "copilot"],
        help="Limit to a specific harness (default: all)",
    )
    discover_parser.add_argument(
        "--since",
        default="7d",
        help="How far back to scan (e.g. 7d, 30d). Default: 7d",
    )
    discover_parser.add_argument(
        "--signals",
        action="store_true",
        help="Extract and display productivity signals for each session",
    )
    discover_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    # Signals — extract productivity signals from a trajectory file
    signals_parser = subparsers.add_parser(
        "signals",
        help="Extract productivity signals from a gptme or Claude Code trajectory (.jsonl)",
    )
    signals_parser.add_argument(
        "path",
        type=Path,
        help="Path to conversation.jsonl",
    )
    signals_output_group = signals_parser.add_mutually_exclusive_group()
    signals_output_group.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON (default: human-readable summary)",
    )
    signals_output_group.add_argument(
        "--grade",
        action="store_true",
        help="Output grade only (float 0.0-1.0)",
    )
    signals_output_group.add_argument(
        "--usage",
        action="store_true",
        help="Output token usage breakdown: input, output, cache_read, cache_create (CC trajectories only)",
    )

    # Post-session — record a session and extract signals in one call
    ps_parser = subparsers.add_parser(
        "post-session",
        help=(
            "Record a completed session: extract signals from trajectory, "
            "determine outcome, append session record."
        ),
    )
    ps_parser.add_argument("--harness", required=True, help="Harness name (claude-code, gptme)")
    ps_parser.add_argument("--model", default="unknown", help="Model name")
    ps_parser.add_argument("--run-type", default="unknown", help="Run type (autonomous, etc.)")
    ps_parser.add_argument(
        "--trigger",
        help="Session trigger: timer, dispatch, manual, spawn",
    )
    ps_parser.add_argument(
        "--category",
        help="Recommended work category from selector (e.g. Thompson sampling). Actual category is inferred from trajectory.",
    )
    ps_parser.add_argument(
        "--exit-code",
        type=int,
        default=0,
        help="Exit code from the agent process (non-zero = failed, 124 = timeout/noop)",
    )
    ps_parser.add_argument("--duration", type=int, default=0, help="Duration in seconds")
    ps_parser.add_argument(
        "--trajectory",
        type=Path,
        help="Path to trajectory .jsonl for signal extraction",
    )
    ps_parser.add_argument("--start-commit", help="Git HEAD SHA before session (for NOOP detect)")
    ps_parser.add_argument("--end-commit", help="Git HEAD SHA after session (for NOOP detect)")
    ps_parser.add_argument(
        "--deliverables",
        nargs="*",
        default=None,
        help=(
            "Explicit deliverables (commit SHAs, PR URLs). "
            "Omit this flag to extract deliverables from the trajectory. "
            "Passing the flag with no values (--deliverables) is treated the same as omitting it "
            "(trajectory extraction still runs); provide at least one value to set explicit deliverables."
        ),
    )
    ps_parser.add_argument("--journal-path", help="Path to journal entry for this session")
    ps_parser.add_argument("--session-id", help="Override auto-generated session ID")
    ps_parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    # Handle signals before constructing SessionStore (no store needed)
    if args.command == "signals":
        p = args.path
        if not p.is_file():
            if p.is_dir():
                print(f"error: {p} is a directory, expected a .jsonl file", file=sys.stderr)
            else:
                print(f"error: {p} not found", file=sys.stderr)
            return 1
        try:
            result = extract_from_path(p)
        except PermissionError:
            print(f"error: cannot read {p}: permission denied", file=sys.stderr)
            return 1
        except UnicodeDecodeError:
            print(f"error: {p} contains non-UTF-8 content", file=sys.stderr)
            return 1
        if args.grade:
            print(f"{result['grade']:.4f}")
            return 0
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        if args.usage:
            usage = result.get("usage")
            if usage:
                if usage.get("total_tokens", 0) > 0:
                    print(
                        f"input={usage['input_tokens']} "
                        f"output={usage['output_tokens']} "
                        f"cache_read={usage['cache_read_tokens']} "
                        f"cache_create={usage['cache_creation_tokens']} "
                        f"total={usage['total_tokens']}"
                    )
                elif usage.get("rate_limit_primary_pct") is not None:
                    primary = usage["rate_limit_primary_pct"]
                    secondary = usage.get("rate_limit_secondary_pct")
                    sec_str = f" secondary={secondary:.1f}%" if secondary is not None else ""
                    print(f"primary={primary:.1f}%{sec_str} (rate limits only; no token counts)")
            return 0
        # Human-readable summary
        tc = result["tool_calls"]
        total_tools = sum(tc.values())
        steps = result.get("steps", 0)
        print(f"Format: {result.get('format', 'gptme')}")
        tools_per_step = f" ({total_tools / steps:.1f} tools/step)" if steps else ""
        print(
            f"Tool calls: {total_tools} in {steps} step(s){tools_per_step} "
            f"({', '.join(f'{t}:{n}' for t, n in sorted(tc.items(), key=lambda x: -x[1])[:5])})"
        )
        print(f"Git commits: {len(result['git_commits'])}")
        unique_writes = len(set(result["file_writes"]))
        total_writes = len(result["file_writes"])
        write_str = (
            str(unique_writes)
            if unique_writes == total_writes
            else f"{unique_writes} unique ({total_writes} total)"
        )
        print(f"File writes: {write_str}")
        print(f"Errors: {result['error_count']}")
        print(f"Retries: {result['retry_count']}")
        print(f"Duration: {result['session_duration_s']}s")
        print(f"Productive: {result['productive']}")
        print(f"Grade: {result['grade']:.4f}")
        inferred = result.get("inferred_category")
        if inferred:
            print(f"Category: {inferred}")
        if result.get("usage"):
            u = result["usage"]
            if "total_tokens" in u:
                print(
                    f"Tokens: {u['total_tokens']:,} total "
                    f"(in={u['input_tokens']:,} out={u['output_tokens']:,} "
                    f"cache_create={u['cache_creation_tokens']:,} "
                    f"cache_read={u['cache_read_tokens']:,})"
                )
            elif u.get("rate_limit_primary_pct") is not None:
                primary = u["rate_limit_primary_pct"]
                secondary = u.get("rate_limit_secondary_pct")
                sec_str = f" secondary={secondary:.1f}%" if secondary is not None else ""
                print(f"Rate limits: primary={primary:.1f}%{sec_str} (no absolute token counts)")
        if result["deliverables"]:
            print("Deliverables:")
            for d in result["deliverables"][:10]:
                print(f"  - {d}")
        return 0

    if args.command == "discover":
        # Parse --since into a date range ending today
        since_val = getattr(args, "since", "7d")
        since_days: int | None
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
        today = date.today()
        start = today - timedelta(days=since_days)

        # Collect sessions per harness
        harness_filter = getattr(args, "harness", None)
        discovered: list[dict] = []

        if harness_filter in (None, "gptme"):
            for p in discover_gptme_sessions(start, today):
                # discover_gptme_sessions returns session directories; resolve to the
                # actual conversation file so extract_from_path can open it.
                jsonl = p / "conversation.jsonl"
                resolved = jsonl if jsonl.exists() else p
                discovered.append({"harness": "gptme", "path": str(resolved)})

        if harness_filter in (None, "claude-code"):
            for p in discover_cc_sessions(start, today):
                discovered.append({"harness": "claude-code", "path": str(p)})

        if harness_filter in (None, "codex"):
            for p in discover_codex_sessions(start, today):
                discovered.append({"harness": "codex", "path": str(p)})

        if harness_filter in (None, "copilot"):
            for p in discover_copilot_sessions(start, today):
                discovered.append({"harness": "copilot", "path": str(p)})

        # Optionally enrich with signals
        if getattr(args, "signals", False):
            for entry in discovered:
                try:
                    result = extract_from_path(Path(entry["path"]))
                    entry["grade"] = result["grade"]
                    entry["productive"] = result["productive"]
                    entry["tool_calls"] = sum(result["tool_calls"].values())
                    entry["git_commits"] = len(result["git_commits"])
                    entry["error_count"] = result["error_count"]
                except Exception as exc:  # noqa: BLE001
                    entry["signals_error"] = str(exc)

        if getattr(args, "json", False):
            print(json.dumps(discovered, indent=2))
        else:
            if not discovered:
                print(f"No sessions found in the last {since_days} day(s).")
                return 0
            # Human-readable table
            harness_width = max(len(e["harness"]) for e in discovered)
            for entry in discovered:
                path_str = entry["path"]
                harness_str = entry["harness"].ljust(harness_width)
                if getattr(args, "signals", False) and "grade" in entry:
                    grade = entry["grade"]
                    prod = "+" if entry["productive"] else "-"
                    tools = entry["tool_calls"]
                    commits = entry["git_commits"]
                    errors = entry["error_count"]
                    print(
                        f"[{prod}] {harness_str}  grade={grade:.2f}"
                        f"  tools={tools} commits={commits} errors={errors}"
                        f"  {path_str}"
                    )
                elif "signals_error" in entry:
                    print(
                        f"[?] {harness_str}  (signals error: {entry['signals_error']})  {path_str}"
                    )
                else:
                    print(f"    {harness_str}  {path_str}")
            print(f"\n{len(discovered)} session(s) found ({start} to {today})")
        return 0

    store = SessionStore(sessions_dir=args.sessions_dir)

    if args.command == "post-session":
        ps = post_session(
            store=store,
            harness=args.harness,
            model=args.model,
            run_type=args.run_type,
            trigger=args.trigger,
            recommended_category=args.category,
            exit_code=args.exit_code,
            duration_seconds=args.duration,
            trajectory_path=args.trajectory,
            start_commit=args.start_commit,
            end_commit=args.end_commit,
            deliverables=args.deliverables or None,
            journal_path=args.journal_path,
            session_id=args.session_id,
        )
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "session_id": ps.record.session_id,
                        "outcome": ps.record.outcome,
                        "grade": ps.grade,
                        "token_count": ps.token_count,
                    }
                )
            )
        else:
            grade_str = f"{ps.grade:.4f}" if ps.grade is not None else "n/a"
            tok_str = f"{ps.token_count:,}" if ps.token_count is not None else "n/a"
            print(
                f"Recorded session {ps.record.session_id}: "
                f"outcome={ps.record.outcome} grade={grade_str} tokens={tok_str}"
            )
        return 0

    if args.command == "append":
        recommended = getattr(args, "recommended_category", None)
        record = SessionRecord(
            harness=args.harness,
            model=args.model,
            run_type=args.run_type,
            category=args.category,
            recommended_category=recommended,
            outcome=args.outcome,
            duration_seconds=args.duration,
            selector_mode=args.selector_mode,
            journal_path=args.journal_path,
            deliverables=args.deliverables or [],
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
