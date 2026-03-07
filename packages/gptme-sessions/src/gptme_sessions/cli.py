"""CLI entry point for gptme-sessions."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import click

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

HARNESS_CHOICES = ["gptme", "claude-code", "codex", "copilot"]


def _discover_all(
    since_days: int = 30,
    harness_filter: str | None = None,
) -> list[dict]:
    """Collect discovered sessions across all harnesses.

    Returns a list of dicts with keys ``harness`` and ``path``.
    Used for fallback display and the ``sync`` command.
    """
    today = date.today()
    start = today - timedelta(days=since_days)
    discovered: list[dict] = []

    if harness_filter in (None, "gptme"):
        for p in discover_gptme_sessions(start, today):
            jsonl = p / "conversation.jsonl"
            resolved = jsonl if jsonl.exists() else p
            discovered.append({"harness": "gptme", "path": resolved})
    if harness_filter in (None, "claude-code"):
        for p in discover_cc_sessions(start, today):
            discovered.append({"harness": "claude-code", "path": p})
    if harness_filter in (None, "codex"):
        for p in discover_codex_sessions(start, today):
            discovered.append({"harness": "codex", "path": p})
    if harness_filter in (None, "copilot"):
        for p in discover_copilot_sessions(start, today):
            discovered.append({"harness": "copilot", "path": p})

    return discovered


def _show_discovery_fallback(since_days: int = 30) -> None:
    """Show discovered sessions when the store has no records.

    Prints a summary grouped by harness, then a hint to run ``sync``.
    """
    discovered = _discover_all(since_days=since_days)
    click.echo("No session records found in store.")
    if not discovered:
        click.echo(
            f"No sessions discovered in the last {since_days} day(s) either.\n"
            "To record sessions, run 'gptme-sessions post-session' after each agent run."
        )
        return

    # Summarize by harness
    counts: dict[str, int] = {}
    for e in discovered:
        counts[e["harness"]] = counts.get(e["harness"], 0) + 1

    click.echo(f"Discovered {len(discovered)} session(s) in the last {since_days} day(s):\n")
    for harness, n in sorted(counts.items()):
        click.echo(f"  {harness:14s}  {n} session(s)")

    click.echo(
        "\nRun 'gptme-sessions sync' to import sessions into the store for analytics."
        "\nRun 'gptme-sessions discover' to list session paths."
    )


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
        s = store.stats()
        if s.get("total", 0) == 0:
            _show_discovery_fallback()
        else:
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

    if show_stats:
        records = store.query(
            model=model,
            run_type=run_type,
            category=category,
            harness=harness,
            outcome=outcome,
            since_days=since_days,
        )
        s = store.stats(records)
        if as_json:
            click.echo(json.dumps(s, indent=2))
        else:
            format_stats(s)
        return

    records = store.query(
        model=model,
        run_type=run_type,
        category=category,
        harness=harness,
        outcome=outcome,
        since_days=since_days,
    )
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
    records = store.query(
        model=model,
        run_type=run_type,
        category=category,
        harness=harness,
        outcome=outcome,
        since_days=since_days,
    )
    s = store.stats(records)
    if as_json:
        click.echo(json.dumps(s, indent=2))
    elif s.get("total", 0) == 0:
        has_filters = any([model, run_type, category, harness, outcome, since_days])
        if has_filters:
            click.echo("No records match your filters.")
        else:
            _show_discovery_fallback(since_days=30)
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
    records = store.query(since_days=since_days)
    analytics = compute_run_analytics(records)
    if as_json:
        click.echo(json.dumps(analytics, indent=2))
    elif analytics.get("total", 0) == 0:
        # --since always acts as a filter (default: 14d). Only show discovery
        # fallback when the store itself is empty, not when the time window
        # simply has no runs.
        if store.query():
            click.echo(f"No runs found in the last {since_days or 14} day(s).")
        else:
            _show_discovery_fallback(since_days=since_days or 14)
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
@click.option(
    "--llm-judge", is_flag=True, help="Run LLM-as-judge scoring (requires anthropic package)"
)
@click.option("--goals", default=None, help="Agent goals for LLM judge (default: generic)")
@click.option("--category", "judge_category", default=None, help="Category hint for LLM judge")
def signals(
    path: Path,
    as_json: bool,
    grade: bool,
    usage: bool,
    llm_judge: bool,
    goals: str | None,
    judge_category: str | None,
) -> None:
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

    # Run LLM judge if requested
    if llm_judge:
        from .judge import judge_from_signals

        judge_kwargs: dict = {}
        if goals:
            judge_kwargs["goals"] = goals
        verdict = judge_from_signals(
            result,
            category=judge_category,
            **judge_kwargs,
        )
        if verdict:
            result["llm_judge"] = verdict
        else:
            click.echo("LLM judge: unavailable (missing API key or anthropic package)", err=True)

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
    if result.get("llm_judge"):
        j = result["llm_judge"]
        click.echo(f"LLM Judge: {j['score']:.2f} ({j['model']}) — {j['reason']}")
    if result["deliverables"]:
        click.echo("Deliverables:")
        for d in result["deliverables"][:10]:
            click.echo(f"  - {d}")


# -- sync --------------------------------------------------------------------


@cli.command()
@click.option(
    "--harness",
    type=click.Choice(HARNESS_CHOICES),
    default=None,
    help="Limit to a specific harness (default: all)",
)
@click.option("--since", default="14d", help="How far back to scan (e.g. 7d, 30d). Default: 14d")
@click.option(
    "--signals",
    "with_signals",
    is_flag=True,
    help="Extract productivity signals from each trajectory (slower but richer)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be imported without writing")
@click.pass_context
def sync(
    ctx: click.Context,
    harness: str | None,
    since: str,
    with_signals: bool,
    dry_run: bool,
) -> None:
    """Discover trajectory files and import them into the session store.

    Scans known trajectory directories for gptme, Claude Code, Codex, and
    Copilot sessions and appends a :class:`~gptme_sessions.record.SessionRecord`
    for each one not already in the store.

    Use ``--signals`` to extract productivity signals (outcome, duration,
    deliverables) from each trajectory.  This is slower but produces richer
    records suitable for ``stats`` and ``runs`` analytics.

    Re-running ``sync`` is safe: sessions already in the store (matched by
    trajectory path) are skipped.
    """
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    since_days = _parse_since(since) or 14
    discovered = _discover_all(since_days=since_days, harness_filter=harness)

    if not discovered:
        click.echo(f"No sessions found in the last {since_days} day(s).")
        return

    # Build a set of already-imported paths for deduplication.
    # We use journal_path to store the trajectory path when syncing.
    existing_paths = {r.journal_path for r in store.load_all() if r.journal_path}

    imported = 0
    skipped = 0
    for entry in discovered:
        path_str = str(entry["path"])

        if path_str in existing_paths:
            skipped += 1
            continue

        record_kwargs: dict = {
            "harness": entry["harness"],
            "journal_path": path_str,  # used for deduplication on re-sync
        }

        if with_signals:
            traj_path = entry["path"]
            if traj_path.is_file():
                try:
                    result = extract_from_path(traj_path)
                    record_kwargs["outcome"] = "productive" if result.get("productive") else "noop"
                    record_kwargs["duration_seconds"] = int(result.get("session_duration_s") or 0)
                    record_kwargs["deliverables"] = result.get("deliverables", [])
                    if result.get("inferred_category"):
                        record_kwargs["category"] = result["inferred_category"]
                except Exception as exc:
                    click.echo(
                        f"  warning: signals extraction failed for {path_str}: {exc}",
                        err=True,
                    )

        if dry_run:
            click.echo(f"  would import: {entry['harness']:14s}  {path_str}")
        else:
            store.append(SessionRecord(**record_kwargs))
            imported += 1

    if dry_run:
        click.echo(
            f"\n{len(discovered)} found, {skipped} already in store, "
            f"{len(discovered) - skipped} would be imported"
        )
    else:
        click.echo(f"Imported {imported} session(s), {skipped} already in store.")


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


# -- judge -------------------------------------------------------------------


@cli.command()
@click.option(
    "--journal-dir",
    type=click.Path(path_type=Path, exists=True),  # type: ignore[type-var]
    default=None,
    help="Path to journal directory (default: ./journal)",
)
@click.option("--last", type=int, default=20, help="Score last N sessions (default: 20)")
@click.option("--goals", default=None, help="Agent goals for LLM judge (default: generic)")
@click.option(
    "--update-store",
    is_flag=True,
    help="Write scores back to session-records.jsonl (matching by session_id)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--dry-run", is_flag=True, help="Show sessions without scoring")
@click.pass_context
def judge(
    ctx: click.Context,
    journal_dir: Path | None,
    last: int,
    goals: str | None,
    update_store: bool,
    as_json: bool,
    dry_run: bool,
) -> None:
    """Score sessions with LLM-as-judge goal-alignment evaluation.

    Reads autonomous session journal entries and evaluates each for strategic
    value. Scores range 0.0-1.0 with a 1-sentence reason.

    With --update-store, writes scores back to session-records.jsonl by matching
    session IDs from journal filenames to stored records.
    """
    import re

    from .judge import DEFAULT_GOALS, DEFAULT_JUDGE_MODEL, judge_session

    if journal_dir is None:
        journal_dir = Path.cwd() / "journal"
    if not journal_dir.is_dir():
        raise click.BadParameter(f"{journal_dir} is not a directory", param_hint="'--journal-dir'")

    # Discover autonomous session entries
    entries: list[Path] = []
    for day_dir in sorted(journal_dir.iterdir()):
        if day_dir.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            entries.extend(sorted(day_dir.glob("autonomous-session-*.md")))
    entries = entries[-last:]

    if not entries:
        click.echo("No autonomous session journal entries found.", err=True)
        return

    click.echo(f"Found {len(entries)} session(s) to {'preview' if dry_run else 'score'}", err=True)

    # Parse session ID from filename
    def extract_sid(p: Path) -> str:
        m = re.match(r"autonomous-session-(\w+)", p.stem)
        return m.group(1) if m else p.stem

    # Parse YAML-like metadata from journal code blocks
    def parse_meta(text: str) -> dict[str, str]:
        m = re.search(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL)
        if not m:
            return {}
        meta = {}
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        return meta

    effective_goals = goals or DEFAULT_GOALS
    results: list[dict] = []

    for entry in entries:
        text = entry.read_text()
        meta = parse_meta(text)
        sid = extract_sid(entry)
        cat = meta.get("category", "unknown")
        outcome = meta.get("outcome", "unknown")
        entry_date = entry.parent.name

        if dry_run:
            click.echo(f"  {sid:<12} {entry_date}  {cat:<14} {outcome}")
            continue

        verdict = judge_session(text, category=cat, goals=effective_goals)
        score = verdict["score"] if verdict else None
        reason = verdict["reason"] if verdict else "N/A"

        results.append(
            {
                "session_id": sid,
                "date": entry_date,
                "category": cat,
                "outcome": outcome,
                "llm_judge_score": score,
                "llm_judge_reason": reason,
                "journal_path": str(entry),
            }
        )

        if not as_json and score is not None:
            click.echo(f"  {sid:<12} {entry_date}  {cat:<14} {score:.2f}  {reason}")

    if dry_run:
        click.echo(f"\n{len(entries)} session(s) (dry run)")
        return

    # Write scores back to store if requested
    if update_store:
        store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
        records = store.load_all()
        score_map = {r["session_id"]: r for r in results if r["llm_judge_score"] is not None}
        updated = 0
        for rec in records:
            if rec.session_id in score_map:
                s = score_map[rec.session_id]
                rec.llm_judge_score = s["llm_judge_score"]
                rec.llm_judge_reason = s["llm_judge_reason"]
                rec.llm_judge_model = DEFAULT_JUDGE_MODEL
                updated += 1
        if updated:
            store.rewrite(records)
            click.echo(f"\nUpdated {updated} record(s) in {store.path}", err=True)
        else:
            click.echo("\nNo matching records found in store to update.", err=True)

    if as_json:
        click.echo(json.dumps(results, indent=2))
    else:
        # Summary stats
        scored = [r for r in results if r["llm_judge_score"] is not None]
        if scored:
            scores = [r["llm_judge_score"] for r in scored]
            click.echo(
                f"\nScored: {len(scored)}/{len(results)}  "
                f"mean={sum(scores) / len(scores):.2f}  "
                f"min={min(scores):.2f}  max={max(scores):.2f}"
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
