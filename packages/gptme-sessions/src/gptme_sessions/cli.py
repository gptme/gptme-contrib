"""CLI entry point for gptme-sessions."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import click

from .discovery import (
    discover_all,
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

HARNESS_CHOICES = ["gptme", "claude-code", "codex", "copilot"]


def _get_records_with_fallback(
    store: SessionStore,
    since_days: int | None = None,
    model: str | None = None,
    run_type: str | None = None,
    category: str | None = None,
    harness: str | None = None,
    outcome: str | None = None,
) -> tuple[list[SessionRecord], bool]:
    """Get records from the JSONL store, falling back to discovery if empty.

    Discovery only triggers when the store has *no records at all* (not when
    filters narrow an existing store to zero results). This prevents silently
    substituting discovered data for filtered store data.

    Returns (records, used_discovery) tuple. When discovery is used and
    produces results, a hint is printed to stderr so the user knows the source.
    """
    # First check if the store has ANY records (unfiltered)
    all_records = store.query()
    if all_records:
        # Store has data — use it with filters, no discovery fallback
        records = store.query(
            model=model,
            run_type=run_type,
            category=category,
            harness=harness,
            outcome=outcome,
            since_days=since_days,
        )
        return records, False

    # Store is truly empty — try discovery
    discover_days = since_days if since_days is not None else 30
    discovered = discover_all(since_days=discover_days)
    if not discovered:
        return [], False

    # Apply the same filters that store.query() would
    if model:
        discovered = [r for r in discovered if r.model_normalized == model or r.model == model]
    if run_type:
        discovered = [r for r in discovered if r.run_type == run_type]
    if category:
        discovered = [r for r in discovered if r.category == category]
    if harness:
        discovered = [r for r in discovered if r.harness == harness]
    if outcome:
        discovered = [r for r in discovered if r.outcome == outcome]

    # Only report discovery if it actually produced results after filtering
    return discovered, len(discovered) > 0


def _parse_since(since: str | None) -> int | None:
    """Parse a --since value like '7d' or '30' into days."""
    if not since:
        return None
    try:
        if since.endswith("d"):
            return int(since[:-1])
        return int(since)
    except ValueError:
        raise click.BadParameter(
            f"invalid value {since!r} (expected e.g. 7d, 30d)",
            param_hint="'--since'",
        )


@click.group(invoke_without_command=True)
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path),  # type: ignore[type-var]
    default=None,
    help="Path to sessions directory (default: ./state/sessions)",
)
@click.pass_context
def cli(ctx: click.Context, sessions_dir: Path | None) -> None:
    """Session tracking and analytics for gptme agents."""
    ctx.ensure_object(dict)
    ctx.obj["sessions_dir"] = sessions_dir
    if ctx.invoked_subcommand is None:
        store = SessionStore(sessions_dir=sessions_dir)
        records, used_discovery = _get_records_with_fallback(store)
        if used_discovery:
            click.echo("(auto-discovered from session directories)\n", err=True)
        s = store.stats(records)
        format_stats(s)


# -- Shared filter options for query/stats -----------------------------------


def _filter_options(func):  # type: ignore[no-untyped-def,unused-ignore]
    """Decorator adding common filter options to a command."""
    for option in reversed(
        [
            click.option("--model", default=None, help="Filter by model (e.g. opus, sonnet)"),
            click.option("--run-type", default=None, help="Filter by run type"),
            click.option("--category", default=None, help="Filter by category"),
            click.option("--harness", default=None, help="Filter by harness"),
            click.option("--outcome", default=None, help="Filter by outcome"),
            click.option("--since", default=None, help="Filter by recency (e.g. 7d, 30d)"),
            click.option("--json", "as_json", is_flag=True, help="Output as JSON"),
        ]
    ):
        func = option(func)
    return func


# -- query -------------------------------------------------------------------


@cli.command()
@_filter_options
@click.option("--stats", "show_stats", is_flag=True, help="Show summary statistics")
@click.pass_context
def query(
    ctx: click.Context,
    model: str | None,
    run_type: str | None,
    category: str | None,
    harness: str | None,
    outcome: str | None,
    since: str | None,
    as_json: bool,
    show_stats: bool,
) -> None:
    """Query session records."""
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    since_days = _parse_since(since)

    records, used_discovery = _get_records_with_fallback(
        store,
        since_days=since_days,
        model=model,
        run_type=run_type,
        category=category,
        harness=harness,
        outcome=outcome,
    )
    if used_discovery:
        click.echo("(auto-discovered from session directories)\n", err=True)

    if show_stats:
        s = store.stats(records)
        if as_json:
            click.echo(json.dumps(s, indent=2))
        else:
            format_stats(s)
        return
    if as_json:
        click.echo(json.dumps([r.to_dict() for r in records], indent=2))
    else:
        for r in records:
            status = "+" if r.outcome == "productive" else "-"
            cat = r.category or "?"
            dur = f"{r.duration_seconds // 60:3d}m" if r.duration_seconds > 0 else "   ?"
            click.echo(
                f"[{status}] {r.timestamp[:16]}  {(r.model_normalized or 'unknown'):8s}  "
                f"{(r.run_type or 'unknown'):12s}  {cat:14s}  {dur}  {r.outcome}"
            )
        click.echo(f"\n{len(records)} records")


# -- stats -------------------------------------------------------------------


@cli.command()
@_filter_options
@click.pass_context
def stats(
    ctx: click.Context,
    model: str | None,
    run_type: str | None,
    category: str | None,
    harness: str | None,
    outcome: str | None,
    since: str | None,
    as_json: bool,
) -> None:
    """Show summary statistics."""
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    since_days = _parse_since(since)
    records, used_discovery = _get_records_with_fallback(
        store,
        since_days=since_days,
        model=model,
        run_type=run_type,
        category=category,
        harness=harness,
        outcome=outcome,
    )
    if used_discovery:
        click.echo("(auto-discovered from session directories)\n", err=True)
    s = store.stats(records)
    if as_json:
        click.echo(json.dumps(s, indent=2))
    else:
        format_stats(s)


# -- runs --------------------------------------------------------------------


@cli.command()
@click.option("--since", default="14d", help="Time window (e.g. 7d, 30d)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def runs(ctx: click.Context, since: str, as_json: bool) -> None:
    """Run analytics (duration, NOOP rate, trends)."""
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    since_days = _parse_since(since)
    records, used_discovery = _get_records_with_fallback(store, since_days=since_days)
    if used_discovery:
        click.echo("(auto-discovered from session directories)\n", err=True)
    analytics = compute_run_analytics(records)
    if as_json:
        click.echo(json.dumps(analytics, indent=2))
    else:
        format_run_analytics(analytics)


# -- append ------------------------------------------------------------------


@cli.command()
@click.option("--harness", default="unknown", help="Harness name")
@click.option("--model", default="unknown", help="Model name")
@click.option("--run-type", default="autonomous", help="Run type")
@click.option("--category", default=None, help="Category (e.g. code, content)")
@click.option("--outcome", default="unknown", help="Session outcome")
@click.option("--duration", type=int, default=0, help="Duration in seconds")
@click.option("--selector-mode", default=None, help="Selector mode used")
@click.option("--journal-path", default=None, help="Path to journal entry")
@click.option("--deliverables", multiple=True, help="Commit SHAs, PR URLs")
@click.pass_context
def append(
    ctx: click.Context,
    harness: str,
    model: str,
    run_type: str,
    category: str | None,
    outcome: str,
    duration: int,
    selector_mode: str | None,
    journal_path: str | None,
    deliverables: tuple[str, ...],
) -> None:
    """Append a session record."""
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    record = SessionRecord(
        harness=harness,
        model=model,
        run_type=run_type,
        category=category,
        outcome=outcome,
        duration_seconds=duration,
        selector_mode=selector_mode,
        journal_path=journal_path,
        deliverables=list(deliverables),
    )
    path = store.append(record)
    click.echo(f"Appended session {record.session_id} to {path}")


# -- discover ----------------------------------------------------------------


@cli.command()
@click.option(
    "--harness",
    type=click.Choice(HARNESS_CHOICES),
    default=None,
    help="Limit to a specific harness (default: all)",
)
@click.option("--since", default="7d", help="How far back to scan (e.g. 7d, 30d). Default: 7d")
@click.option(
    "--signals", is_flag=True, help="Extract and display productivity signals for each session"
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def discover(
    harness: str | None,
    since: str,
    signals: bool,
    as_json: bool,
) -> None:
    """Discover trajectory files from gptme, Claude Code, Codex, and Copilot harnesses."""
    since_days = _parse_since(since) or 7
    today = date.today()
    start = today - timedelta(days=since_days)

    discovered: list[dict] = []

    if harness in (None, "gptme"):
        for p in discover_gptme_sessions(start, today):
            jsonl = p / "conversation.jsonl"
            resolved = jsonl if jsonl.exists() else p
            discovered.append({"harness": "gptme", "path": str(resolved)})

    if harness in (None, "claude-code"):
        for p in discover_cc_sessions(start, today):
            discovered.append({"harness": "claude-code", "path": str(p)})

    if harness in (None, "codex"):
        for p in discover_codex_sessions(start, today):
            discovered.append({"harness": "codex", "path": str(p)})

    if harness in (None, "copilot"):
        for p in discover_copilot_sessions(start, today):
            discovered.append({"harness": "copilot", "path": str(p)})

    # Optionally enrich with signals
    if signals:
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

    if as_json:
        click.echo(json.dumps(discovered, indent=2))
    else:
        if not discovered:
            click.echo(f"No sessions found in the last {since_days} day(s).")
            return
        harness_width = max(len(e["harness"]) for e in discovered)
        for entry in discovered:
            path_str = entry["path"]
            harness_str = entry["harness"].ljust(harness_width)
            if signals and "grade" in entry:
                grade = entry["grade"]
                prod = "+" if entry["productive"] else "-"
                tools = entry["tool_calls"]
                commits = entry["git_commits"]
                errors = entry["error_count"]
                click.echo(
                    f"[{prod}] {harness_str}  grade={grade:.2f}"
                    f"  tools={tools} commits={commits} errors={errors}"
                    f"  {path_str}"
                )
            elif "signals_error" in entry:
                click.echo(
                    f"[?] {harness_str}  (signals error: {entry['signals_error']})  {path_str}"
                )
            else:
                click.echo(f"    {harness_str}  {path_str}")
        click.echo(f"\n{len(discovered)} session(s) found ({start} to {today})")


# -- signals -----------------------------------------------------------------


@cli.command()
@click.argument("path", type=click.Path(path_type=Path))  # type: ignore[type-var]
@click.option(
    "--json", "as_json", is_flag=True, help="Output as JSON (default: human-readable summary)"
)
@click.option("--grade", is_flag=True, help="Output grade only (float 0.0-1.0)")
@click.option("--usage", is_flag=True, help="Output token usage breakdown")
def signals(path: Path, as_json: bool, grade: bool, usage: bool) -> None:
    """Extract productivity signals from a gptme or Claude Code trajectory (.jsonl)."""
    # Validate mutual exclusivity
    flags = sum([as_json, grade, usage])
    if flags > 1:
        raise click.UsageError("--json, --grade, and --usage are mutually exclusive")

    if not path.is_file():
        if path.is_dir():
            raise click.BadParameter(
                f"{path} is a directory, expected a .jsonl file", param_hint="'PATH'"
            )
        else:
            raise click.BadParameter(f"{path} not found", param_hint="'PATH'")
    try:
        result = extract_from_path(path)
    except PermissionError:
        raise click.ClickException(f"cannot read {path}: permission denied")
    except UnicodeDecodeError:
        raise click.ClickException(f"{path} contains non-UTF-8 content")

    if grade:
        click.echo(f"{result['grade']:.4f}")
        return
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if usage:
        u = result.get("usage")
        if u:
            if u.get("total_tokens", 0) > 0:
                click.echo(
                    f"input={u['input_tokens']} "
                    f"output={u['output_tokens']} "
                    f"cache_read={u['cache_read_tokens']} "
                    f"cache_create={u['cache_creation_tokens']} "
                    f"total={u['total_tokens']}"
                )
            elif u.get("rate_limit_primary_pct") is not None:
                primary = u["rate_limit_primary_pct"]
                secondary = u.get("rate_limit_secondary_pct")
                sec_str = f" secondary={secondary:.1f}%" if secondary is not None else ""
                click.echo(
                    f"Rate limits: primary={primary:.1f}%{sec_str} (no absolute token counts)"
                )
        return

    # Human-readable summary
    tc = result["tool_calls"]
    total_tools = sum(tc.values())
    steps = result.get("steps", 0)
    click.echo(f"Format: {result.get('format', 'gptme')}")
    tools_per_step = f" ({total_tools / steps:.1f} tools/step)" if steps else ""
    click.echo(
        f"Tool calls: {total_tools} in {steps} step(s){tools_per_step} "
        f"({', '.join(f'{t}:{n}' for t, n in sorted(tc.items(), key=lambda x: -x[1])[:5])})"
    )
    click.echo(f"Git commits: {len(result['git_commits'])}")
    unique_writes = len(set(result["file_writes"]))
    total_writes = len(result["file_writes"])
    write_str = (
        str(unique_writes)
        if unique_writes == total_writes
        else f"{unique_writes} unique ({total_writes} total)"
    )
    click.echo(f"File writes: {write_str}")
    click.echo(f"Errors: {result['error_count']}")
    click.echo(f"Retries: {result['retry_count']}")
    click.echo(f"Duration: {result['session_duration_s']}s")
    click.echo(f"Productive: {result['productive']}")
    click.echo(f"Grade: {result['grade']:.4f}")
    if result.get("usage"):
        u = result["usage"]
        if "total_tokens" in u:
            click.echo(
                f"Tokens: {u['total_tokens']:,} total "
                f"(in={u['input_tokens']:,} out={u['output_tokens']:,} "
                f"cache_create={u['cache_creation_tokens']:,} "
                f"cache_read={u['cache_read_tokens']:,})"
            )
        elif u.get("rate_limit_primary_pct") is not None:
            primary = u["rate_limit_primary_pct"]
            secondary = u.get("rate_limit_secondary_pct")
            sec_str = f" secondary={secondary:.1f}%" if secondary is not None else ""
            click.echo(f"Rate limits: primary={primary:.1f}%{sec_str} (no absolute token counts)")
    if result["deliverables"]:
        click.echo("Deliverables:")
        for d in result["deliverables"][:10]:
            click.echo(f"  - {d}")


# -- post-session ------------------------------------------------------------


@cli.command("post-session")
@click.option(
    "--harness",
    required=True,
    type=click.Choice(HARNESS_CHOICES),
    help="Harness name (claude-code, gptme, codex, copilot)",
)
@click.option("--model", default="unknown", help="Model name")
@click.option("--run-type", default="unknown", help="Run type (autonomous, etc.)")
@click.option("--trigger", default=None, help="Session trigger: timer, dispatch, manual, spawn")
@click.option("--category", default=None, help="Work category (code, triage, ...)")
@click.option(
    "--exit-code",
    type=int,
    default=0,
    help="Exit code from the agent process (non-zero = failed, 124 = timeout/noop)",
)
@click.option("--duration", type=int, default=0, help="Duration in seconds")
@click.option(
    "--trajectory",
    type=click.Path(path_type=Path),  # type: ignore[type-var]
    default=None,
    help="Path to trajectory .jsonl for signal extraction",
)
@click.option("--start-commit", default=None, help="Git HEAD SHA before session (for NOOP detect)")
@click.option("--end-commit", default=None, help="Git HEAD SHA after session (for NOOP detect)")
@click.option(
    "--deliverables",
    "deliverables_raw",
    multiple=True,
    help="Explicit deliverables (commit SHAs, PR URLs). Omit to extract from trajectory.",
)
@click.option("--journal-path", default=None, help="Path to journal entry for this session")
@click.option("--session-id", default=None, help="Override auto-generated session ID")
@click.option("--json", "as_json", is_flag=True, help="Output result as JSON")
@click.pass_context
def post_session_cmd(
    ctx: click.Context,
    harness: str,
    model: str,
    run_type: str,
    trigger: str | None,
    category: str | None,
    exit_code: int,
    duration: int,
    trajectory: Path | None,
    start_commit: str | None,
    end_commit: str | None,
    deliverables_raw: tuple[str, ...],
    journal_path: str | None,
    session_id: str | None,
    as_json: bool,
) -> None:
    """Record a completed session: extract signals, determine outcome, append record."""
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    deliverables = list(deliverables_raw) if deliverables_raw else None
    ps = post_session(
        store=store,
        harness=harness,
        model=model,
        run_type=run_type,
        trigger=trigger,
        category=category,
        exit_code=exit_code,
        duration_seconds=duration,
        trajectory_path=trajectory,
        start_commit=start_commit,
        end_commit=end_commit,
        deliverables=deliverables,
        journal_path=journal_path,
        session_id=session_id,
    )
    if as_json:
        click.echo(
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
        click.echo(
            f"Recorded session {ps.record.session_id}: "
            f"outcome={ps.record.outcome} grade={grade_str} tokens={tok_str}"
        )


def main() -> int:
    """Entry point for console_scripts (backward-compatible wrapper).

    Uses standalone_mode=False so Click returns instead of calling sys.exit(),
    preserving the return-code contract expected by callers and tests.
    """
    try:
        result = cli(standalone_mode=False)
        if isinstance(result, int):
            return result
    except SystemExit as e:
        return int(e.code) if e.code else 0
    except click.exceptions.ClickException as e:
        e.show()
        return e.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
