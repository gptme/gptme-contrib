"""CLI entry point for gptme-sessions."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import click

# fcntl is POSIX-only; on Windows we skip locking.
try:
    import fcntl as _fcntl

    _has_fcntl = True
except ImportError:
    _has_fcntl = False

from .discovery import (
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
    extract_cc_model,
    extract_project,
    extract_session_name,
    parse_gptme_config,
    session_date_from_path,
    session_datetime_from_path,
)
from .post_session import VALID_AB_GROUPS, VALID_CONTEXT_TIERS, post_session
from .record import SessionRecord, normalize_run_type
from .signals import extract_from_path
from .store import (
    SessionStore,
    compute_run_analytics,
    format_run_analytics,
    format_stats,
)

logger = logging.getLogger(__name__)

HARNESS_CHOICES = ["gptme", "claude-code", "codex", "copilot"]


def _discover_all(
    since_days: int = 30,
    harness_filter: str | None = None,
) -> list[dict]:
    """Collect discovered sessions across all harnesses, sorted chronologically.

    Returns a list of dicts with keys ``harness``, ``path``, and
    ``session_date`` (a :class:`datetime.date` or ``None``).
    Dicts for ``gptme`` and ``claude-code`` harnesses also include ``model``
    (may be ``None`` if extraction fails); ``codex`` and ``copilot`` entries
    do not include ``model``.  Callers should use ``.get("model")`` accordingly.
    Results are sorted oldest-first across all harnesses so callers get a
    unified chronological view rather than harness-grouped output.
    Used for fallback display and the ``sync`` command.
    """
    today = date.today()
    start = today - timedelta(days=since_days)
    discovered: list[dict] = []

    if harness_filter in (None, "gptme"):
        for p in discover_gptme_sessions(start, today):
            jsonl = p / "conversation.jsonl"
            resolved = jsonl if jsonl.exists() else p
            model = parse_gptme_config(p).get("model") or None
            discovered.append(
                {
                    "harness": "gptme",
                    "path": resolved,
                    "model": model,
                    "session_date": session_date_from_path("gptme", resolved),
                    "session_name": extract_session_name("gptme", resolved),
                    "project": extract_project("gptme", resolved),
                }
            )
    if harness_filter in (None, "claude-code"):
        for p in discover_cc_sessions(start, today):
            model = extract_cc_model(p)
            discovered.append(
                {
                    "harness": "claude-code",
                    "path": p,
                    "model": model,
                    "session_date": session_date_from_path("claude-code", p),
                    "session_name": extract_session_name("claude-code", p),
                    "project": extract_project("claude-code", p),
                }
            )
    if harness_filter in (None, "codex"):
        for p in discover_codex_sessions(start, today):
            discovered.append(
                {
                    "harness": "codex",
                    "path": p,
                    "session_date": session_date_from_path("codex", p),
                    "session_name": extract_session_name("codex", p),
                    "project": extract_project("codex", p),
                }
            )
    if harness_filter in (None, "copilot"):
        for p in discover_copilot_sessions(start, today):
            discovered.append(
                {
                    "harness": "copilot",
                    "path": p,
                    "session_date": session_date_from_path("copilot", p),
                    "session_name": extract_session_name("copilot", p),
                    "project": extract_project("copilot", p),
                }
            )

    # Sort chronologically across harnesses; entries without a date sort last.
    discovered.sort(key=lambda e: e.get("session_date") or date.max)
    return discovered


def _count_unsynced(
    store: SessionStore,
    records: list[SessionRecord] | None = None,
    since_days: int = 14,
) -> int:
    """Count sessions discovered in the last *since_days* days not yet in the store.

    Pass *records* to avoid a redundant ``store.load_all()`` call when the
    caller has already loaded them (e.g. for ``store.stats()``).

    Uses the same path-matching logic as the ``sync`` command so the count
    accurately reflects what ``sync`` would import for the same *since_days*
    window.  Note that ``sync --since`` accepts custom windows (e.g. 90d), so
    running ``sync`` with a wider window may import sessions not counted here.
    """
    discovered = _discover_all(since_days=since_days)
    if not discovered:
        return 0
    if records is None:
        records = store.load_all()
    existing_paths = {r.journal_path for r in records if r.journal_path} | {
        r.trajectory_path for r in records if r.trajectory_path
    }
    return sum(1 for e in discovered if str(e["path"]) not in existing_paths)


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
    """Parse a --since value like '7d', '1h', '30', or 'all' into days (None = no filter).

    Hours are converted to days (minimum 1 day for store queries).
    """
    if not since:
        return None
    if since.lower() == "all":
        return None
    try:
        if since.endswith("d"):
            return int(since[:-1])
        if since.endswith("h"):
            import math

            return max(1, math.ceil(int(since[:-1]) / 24))
        return int(since)
    except ValueError:
        raise click.BadParameter(
            f"invalid value {since!r} (expected e.g. 1h, 7d, 30d, or 'all')",
            param_hint="'--since'",
        )


@click.group(invoke_without_command=True)
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path),  # type: ignore[type-var]
    default=None,
    help="Path to sessions directory (default: ~/.local/share/gptme-sessions/)",
)
@click.pass_context
def cli(ctx: click.Context, sessions_dir: Path | None) -> None:
    """Session tracking and analytics for agents. Supports trajectories from gptme, Claude Code, Codex, and Copilot."""
    ctx.ensure_object(dict)
    ctx.obj["sessions_dir"] = sessions_dir
    if ctx.invoked_subcommand is None:
        _unsync_window = 14  # days to scan for unsynced sessions
        _default_since = 30  # default stats window
        store = SessionStore(sessions_dir=sessions_dir)
        records = store.load_all()

        # Default to last 30 days for top-level stats
        recent = store.query(since_days=_default_since)
        s = store.stats(recent)
        if s.get("total", 0) == 0:
            if records:
                # Records exist but all fall outside the default window
                click.echo(
                    f"No records in the last {_default_since} days. "
                    "Use 'gptme-sessions stats --since all' for all-time data."
                )
            else:
                _show_discovery_fallback()
        else:
            click.echo(
                f"Last {_default_since} days (use 'gptme-sessions stats --since all' for all-time):\n"
            )
            format_stats(s)
            unsynced = _count_unsynced(store, records=records, since_days=_unsync_window)
            if unsynced > 0:
                click.echo(
                    f"\nTip: {unsynced} new session(s) available in the last {_unsync_window} days. "
                    "Run 'gptme-sessions sync' to import."
                )
            else:
                click.echo("\nTip: Run 'gptme-sessions sync' to keep the store up to date.")


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
            click.option(
                "--project", default=None, help="Filter by project name (substring match)"
            ),
            click.option(
                "--since",
                default=None,
                help="Filter by recency (e.g. 7d, 30d, or 'all')",
            ),
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
    project: str | None,
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
            project=project,
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
        project=project,
    )
    if as_json:
        click.echo(json.dumps([r.to_dict() for r in records], indent=2))
    else:
        for r in records:
            status = "+" if r.outcome == "productive" else "-"
            cat = r.category or "?"
            dur = f"{r.duration_seconds // 60:3d}m" if r.duration_seconds > 0 else "   ?"
            # Show date only (not time) for compactness
            date_str = r.timestamp[:10] if r.timestamp else "????"
            # Session name or short ID
            name = r.session_name or r.session_id[:8]
            # Project: last path component
            proj = ""
            if r.project:
                proj = r.project.rstrip("/").rsplit("/", 1)[-1] if "/" in r.project else r.project
            click.echo(
                f"[{status}] {date_str}  {name:20s}  {(r.model_normalized or 'unknown'):8s}  "
                f"{cat:14s}  {dur}  {proj}"
            )
        click.echo(f"\n{len(records)} records")


# -- show --------------------------------------------------------------------


@cli.command()
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def show(ctx: click.Context, session_id: str, as_json: bool) -> None:
    """Show details for a single session record by ID prefix.

    SESSION_ID is a full or prefix of a session ID (e.g. 'a1b2c3d4' or 'a1b2').
    """
    if not session_id:
        raise click.UsageError("Session ID must not be empty.")
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    records = store.load_all()
    matches = [r for r in records if r.session_id.startswith(session_id)]
    if not matches:
        raise click.ClickException(
            f"No session found matching '{session_id}'. "
            "Run 'gptme-sessions query' to list available session IDs."
        )
    if len(matches) > 1:
        raise click.ClickException(
            f"Ambiguous prefix '{session_id}' matches {len(matches)} sessions: "
            + ", ".join(r.session_id for r in matches)
            + ". Run 'gptme-sessions query' to list available session IDs."
        )
    record = matches[0]

    if as_json:
        click.echo(json.dumps(record.to_dict(), indent=2))
        return

    status = "+" if record.outcome == "productive" else "-"
    click.echo(f"[{status}] {record.session_id}  {record.timestamp[:10]}")
    if record.session_name:
        click.echo(f"  {'Name:':<14}{record.session_name}")
    if record.project:
        click.echo(f"  {'Project:':<14}{record.project}")
    click.echo(f"  {'Harness:':<14}{record.harness or 'unknown'}")
    click.echo(f"  {'Model:':<14}{record.model or 'unknown'}")
    click.echo(f"  {'Run type:':<14}{record.run_type or 'unknown'}")
    click.echo(f"  {'Outcome:':<14}{record.outcome}")
    if record.duration_seconds:
        _s = record.duration_seconds
        _h, _rem = divmod(_s, 3600)
        _m, _sec = divmod(_rem, 60)
        dur_str = f"{_h}h {_m}m {_sec}s" if _h else f"{_m}m {_sec}s"
        click.echo(f"  {'Duration:':<14}{dur_str}")
    if record.category:
        click.echo(f"  {'Category:':<14}{record.category}")
    if record.recommended_category:
        click.echo(f"  {'Recommended:':<14}{record.recommended_category}")
    if record.selector_mode:
        click.echo(f"  {'Selector:':<14}{record.selector_mode}")
    if record.token_count is not None:
        click.echo(f"  {'Tokens:':<14}{record.token_count:,}")
    if record.llm_judge_score is not None:
        click.echo(f"  {'Judge score:':<14}{record.llm_judge_score:.2f}")
    if record.llm_judge_reason:
        click.echo(f"  {'Judge reason:':<14}{record.llm_judge_reason}")
    if record.llm_judge_model:
        click.echo(f"  {'Judge model:':<14}{record.llm_judge_model}")
    if record.trigger:
        click.echo(f"  {'Trigger:':<14}{record.trigger}")
    if record.journal_path:
        click.echo(f"  {'Journal:':<14}{record.journal_path}")
    if record.deliverables:
        click.echo("  Deliverables:")
        for d in record.deliverables:
            click.echo(f"    - {d}")


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
    project: str | None,
    since: str | None,
    as_json: bool,
) -> None:
    """Show summary statistics.

    Defaults to last 30 days. Use --since all for all-time stats.
    """
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    # Default to 30d when no --since specified
    since_days = _parse_since(since) if since else 30
    records = store.query(
        model=model,
        run_type=run_type,
        category=category,
        harness=harness,
        outcome=outcome,
        since_days=since_days,
        project=project,
    )
    s = store.stats(records)
    showed_fallback = False
    if as_json:
        click.echo(json.dumps(s, indent=2))
    elif s.get("total", 0) == 0:
        has_filters = any([model, run_type, category, harness, outcome, project, since])
        if has_filters:
            click.echo("No records match your filters.")
        elif store.load_all():
            # Records exist but all fall outside the implicit 30-day window
            click.echo(
                f"No records in the last {since_days} days. Use --since all for all-time data."
            )
        else:
            _show_discovery_fallback(since_days=30)
            showed_fallback = True
    else:
        if not since:
            click.echo(f"Last {since_days} days (use --since all for all-time):\n")
        format_stats(s)
    if not as_json and not showed_fallback:
        # Check for unsynced sessions regardless of whether stats matched anything —
        # hint is useful even when results exist (import may add more context).
        # Skip when _show_discovery_fallback already printed a sync recommendation.
        hint_window = since_days if since_days else 30
        unsynced = _count_unsynced(store, since_days=hint_window)
        if unsynced > 0:
            click.echo(
                f"\nHint: {unsynced} session(s) discovered but not synced. "
                "Run 'gptme-sessions sync' to import."
            )


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
    """Append a session record.

    Deprecated: use 'sync' to import sessions from trajectories, or 'annotate'
    to correct metadata on an existing record.
    """
    click.echo(
        "Warning: 'append' is deprecated. Use 'sync' to import sessions from trajectories "
        "or 'annotate' to correct metadata on an existing record.",
        err=True,
    )
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


# -- annotate ----------------------------------------------------------------


@cli.command()
@click.argument("session_id")
@click.option("--model", default=None, help="Override model name")
@click.option("--harness", default=None, help="Override harness")
@click.option("--run-type", default=None, help="Override run type")
@click.option("--category", default=None, help="Override category")
@click.option(
    "--outcome",
    default=None,
    type=click.Choice(["productive", "noop", "failed", "unknown"]),
    help="Override outcome",
)
@click.option(
    "--duration",
    type=click.IntRange(min=0),
    default=None,
    help="Override duration in seconds (must be non-negative)",
)
@click.option("--journal-path", default=None, help="Override journal path")
@click.option(
    "--selector-mode",
    default=None,
    help="Override selector mode (e.g. scored, llm-context)",
)
@click.option(
    "--trigger",
    default=None,
    type=click.Choice(["timer", "dispatch", "manual", "spawn"]),
    help="Override trigger",
)
@click.option(
    "--token-count", type=click.IntRange(min=0), default=None, help="Override token count"
)
@click.option(
    "--recommended-category",
    default=None,
    help="Override recommended category (from Thompson sampling / CASCADE)",
)
@click.option("--add-deliverable", multiple=True, help="Add deliverable(s) to existing list")
@click.option(
    "--json", "as_json", is_flag=True, help="Output updated record as JSON after applying changes"
)
@click.pass_context
def annotate(
    ctx: click.Context,
    session_id: str,
    model: str | None,
    harness: str | None,
    run_type: str | None,
    category: str | None,
    outcome: str | None,
    duration: int | None,
    journal_path: str | None,
    selector_mode: str | None,
    trigger: str | None,
    token_count: int | None,
    recommended_category: str | None,
    add_deliverable: tuple[str, ...],
    as_json: bool,
) -> None:
    """Amend an existing session record by session ID (prefix match supported).

    Useful for manually correcting metadata extracted at sync time — for
    example fixing a misidentified model, reclassifying the outcome, or
    adding a journal path that wasn't set during the session.

    Only fields explicitly supplied are updated; all other fields are left
    unchanged.  To add deliverables without overwriting existing ones, use
    ``--add-deliverable``.  There is currently no option to replace the whole
    deliverables list; edit the session-records.jsonl file directly for that.
    """
    # Validate session_id first — its error message is more precise than the
    # no-op guard, so diagnose it regardless of what other options were passed.
    if not session_id:
        raise click.UsageError("Session ID must not be empty.")

    # Guard: if nothing was supplied, avoid a no-op rewrite (check before touching the store)
    nothing_supplied = (
        model is None
        and harness is None
        and run_type is None
        and category is None
        and outcome is None
        and duration is None
        and journal_path is None
        and selector_mode is None
        and trigger is None
        and token_count is None
        and recommended_category is None
        and not add_deliverable
    )
    if nothing_supplied:
        raise click.UsageError(
            "No fields specified. Provide at least one option to update "
            "(e.g. --model, --outcome, --add-deliverable)."
        )

    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    store.sessions_dir.mkdir(parents=True, exist_ok=True)

    # Hold an exclusive lock to serialise concurrent annotate calls during the
    # load → mutate → rewrite cycle (prevents annotate-vs-annotate clobber).
    # Note: sync/post-session use store.append(), which does not acquire this
    # lock. Fully protecting against annotate+sync races would require locking
    # inside SessionStore itself — that is a future improvement.

    # The lock file is a permanent sentinel — never deleted. This ensures all
    # concurrent annotate calls operate on the same inode, so the flock queue
    # works correctly. Deleting the file would allow a newly arriving process
    # to acquire LOCK_EX on a fresh inode while a blocked waiter holds
    # LOCK_EX on the old inode, breaking mutual exclusion.
    lock_path = store.path.with_name(store.path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        if _has_fcntl:
            _fcntl.flock(lock_file, _fcntl.LOCK_EX)
        try:
            records = store.load_all()

            if not records:
                raise click.ClickException("No session records found in store.")

            # Resolve by prefix — short IDs like "a1b2" are common; IDs are lowercase hex
            matches = [r for r in records if r.session_id.startswith(session_id)]
            if not matches:
                raise click.ClickException(
                    f"No session found with ID prefix {session_id!r}. "
                    "Run 'gptme-sessions query' to list available session IDs."
                )
            if len(matches) > 1:
                ids = ", ".join(r.session_id for r in matches)
                raise click.ClickException(
                    f"Ambiguous prefix {session_id!r} matches {len(matches)} sessions: {ids}"
                )

            record = matches[0]

            # Apply only the fields that were explicitly provided
            if model is not None:
                record.model = model
            if harness is not None:
                record.harness = harness
            if run_type is not None:
                record.run_type = normalize_run_type(run_type)
            if category is not None:
                record.category = category
            if outcome is not None:
                record.outcome = outcome
            if duration is not None:
                record.duration_seconds = duration
            if journal_path is not None:
                record.journal_path = journal_path
            if selector_mode is not None:
                record.selector_mode = selector_mode
            if trigger is not None:
                record.trigger = trigger
            if token_count is not None:
                record.token_count = token_count
            if recommended_category is not None:
                record.recommended_category = recommended_category
            if add_deliverable:
                existing = list(record.deliverables or [])
                for d in add_deliverable:
                    if d not in existing:
                        existing.append(d)
                record.deliverables = existing

            store.rewrite(records)

            if as_json:
                click.echo(json.dumps(record.to_dict(), indent=2, default=str))
            else:
                click.echo(f"Updated session {record.session_id}.")
        finally:
            if _has_fcntl:
                _fcntl.flock(lock_file, _fcntl.LOCK_UN)


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
@click.option("--unsynced", is_flag=True, help="Show only sessions not yet imported into the store")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def discover(
    ctx: click.Context,
    harness: str | None,
    since: str,
    signals: bool,
    unsynced: bool,
    as_json: bool,
) -> None:
    """Discover trajectory files from gptme, Claude Code, Codex, and Copilot harnesses.

    Shows a sync-status indicator for each session:
    [S] already imported into the store, [ ] not yet synced.
    Use --unsynced to list only sessions pending import.
    """
    since_days = _parse_since(since) or 7
    today = date.today()
    start = today - timedelta(days=since_days)

    discovered: list[dict] = []

    if harness in (None, "gptme"):
        for p in discover_gptme_sessions(start, today):
            jsonl = p / "conversation.jsonl"
            resolved = jsonl if jsonl.exists() else p
            discovered.append(
                {
                    "harness": "gptme",
                    "path": str(resolved),
                    "session_date": session_date_from_path("gptme", resolved),
                }
            )

    if harness in (None, "claude-code"):
        for p in discover_cc_sessions(start, today):
            discovered.append(
                {
                    "harness": "claude-code",
                    "path": str(p),
                    "session_date": session_date_from_path("claude-code", p),
                }
            )

    if harness in (None, "codex"):
        for p in discover_codex_sessions(start, today):
            discovered.append(
                {
                    "harness": "codex",
                    "path": str(p),
                    "session_date": session_date_from_path("codex", p),
                }
            )

    if harness in (None, "copilot"):
        for p in discover_copilot_sessions(start, today):
            discovered.append(
                {
                    "harness": "copilot",
                    "path": str(p),
                    "session_date": session_date_from_path("copilot", p),
                }
            )

    # Sort chronologically across all harnesses; entries without a date sort last.
    discovered.sort(key=lambda e: e.get("session_date") or date.max)

    # Mark each entry as synced or not by cross-referencing the store.
    # Normalize paths so symlinks/relative paths don't cause false mismatches.
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
    records = store.load_all()
    existing_paths = {str(Path(r.journal_path).resolve()) for r in records if r.journal_path}
    for entry in discovered:
        entry["synced"] = str(Path(entry["path"]).resolve()) in existing_paths

    total_discovered = len(discovered)

    # Apply --unsynced filter before signal extraction (avoid wasted work).
    if unsynced:
        discovered = [e for e in discovered if not e["synced"]]

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
        # Always emit a wrapper object so the schema is consistent regardless
        # of whether --unsynced is used.  Consumers can always read
        # ``output["sessions"]`` without branching on the flag.
        # ``total_discovered`` lets callers distinguish "nothing found in
        # window" (total_discovered=0, sessions=[]) from "all already synced"
        # (total_discovered>0, sessions=[]) when --unsynced is active.
        click.echo(
            json.dumps(
                {"sessions": discovered, "total_discovered": total_discovered},
                indent=2,
                default=str,
            )
        )
    else:
        if not discovered:
            if unsynced and total_discovered > 0:
                click.echo(
                    f"All {total_discovered} session(s) in the last {since_days} day(s) are already synced."
                )
            else:
                click.echo(f"No sessions found in the last {since_days} day(s).")
            return
        harness_width = max(len(e["harness"]) for e in discovered)
        for entry in discovered:
            path_str = entry["path"]
            harness_str = entry["harness"].ljust(harness_width)
            sync_flag = "S" if entry["synced"] else " "
            date_str = str(entry["session_date"]) if entry.get("session_date") else "????"
            if signals and "grade" in entry:
                grade = entry["grade"]
                prod = "+" if entry["productive"] else "-"
                tools = entry["tool_calls"]
                commits = entry["git_commits"]
                errors = entry["error_count"]
                click.echo(
                    f"[{sync_flag}][{prod}] {date_str}  {harness_str}  grade={grade:.2f}"
                    f"  tools={tools} commits={commits} errors={errors}"
                    f"  {path_str}"
                )
            elif "signals_error" in entry:
                click.echo(
                    f"[{sync_flag}][?] {date_str}  {harness_str}"
                    f"  (signals error: {entry['signals_error']})  {path_str}"
                )
            else:
                click.echo(f"[{sync_flag}]   {date_str}  {harness_str}  {path_str}")
        if unsynced:
            synced_skipped = total_discovered - len(discovered)
            footer = (
                f" ({total_discovered} total, {synced_skipped} already synced)"
                if synced_skipped
                else ""
            )
        else:
            unsynced_count = sum(1 for e in discovered if not e["synced"])
            synced_count = len(discovered) - unsynced_count
            footer = f", {synced_count} synced, {unsynced_count} pending"
        click.echo(f"\n{len(discovered)} session(s) found ({start} to {today})" + footer)


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

    # Run LLM judge if requested (skip with --grade/--usage — result not displayed in those modes)
    if llm_judge and not (grade or usage):
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


# -- transcript --------------------------------------------------------------


@cli.command()
@click.argument("path", type=click.Path(path_type=Path))  # type: ignore[type-var]
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (default: human-readable)")
@click.option(
    "--messages-only",
    is_flag=True,
    help="Output only the messages array (implies --json)",
)
def transcript(path: Path, as_json: bool, messages_only: bool) -> None:
    """Read a trajectory file and output a normalized session transcript.

    PATH is the path to a harness JSONL file (gptme conversation.jsonl,
    Claude Code session UUID.jsonl, Codex rollout.jsonl, or Copilot
    events.jsonl). The format is auto-detected.

    Use --json for the full stable machine-readable contract (schema_version,
    session metadata, and messages array). Use --messages-only for just the
    messages array (useful for piping into other tools).

    The JSON output is a stable, versioned contract (schema_version=1) that
    external consumers can depend on across harness updates.
    """
    from .transcript import read_transcript

    if not path.is_file():
        if path.is_dir():
            raise click.BadParameter(
                f"{path} is a directory, expected a .jsonl file", param_hint="'PATH'"
            )
        raise click.BadParameter(f"{path} not found", param_hint="'PATH'")

    try:
        t = read_transcript(path)
    except PermissionError:
        raise click.ClickException(f"cannot read {path}: permission denied")
    except UnicodeDecodeError:
        raise click.ClickException(f"{path} contains non-UTF-8 content")

    if messages_only:
        click.echo(json.dumps([m.to_dict() for m in t.messages], indent=2))
        return

    if as_json:
        click.echo(t.to_json())
        return

    # Human-readable summary
    click.echo(f"Harness:      {t.harness}")
    click.echo(f"Session ID:   {t.session_id}")
    if t.session_name:
        click.echo(f"Session name: {t.session_name}")
    if t.project:
        click.echo(f"Project:      {t.project}")
    if t.model:
        click.echo(f"Model:        {t.model}")
    if t.started_at:
        click.echo(f"Started at:   {t.started_at}")
    if t.last_activity:
        click.echo(f"Last activity: {t.last_activity}")
    click.echo(f"Messages:     {len(t.messages)}")
    click.echo(f"Capabilities: {', '.join(t.capabilities) or 'none'}")
    click.echo(f"Schema:       v{t.schema_version}")

    if t.messages:
        click.echo("\nTranscript (first 5 messages):")
        for msg in t.messages[:5]:
            ts_str = f"[{msg.timestamp[:19]}] " if msg.timestamp else ""
            if msg.tool_name:
                click.echo(
                    f"  {ts_str}{msg.role} → {msg.tool_name}({json.dumps(msg.tool_input or {})})"
                )
            elif msg.role == "tool_result":
                content_preview = (msg.content or "")[:80].replace("\n", "\\n")
                err_flag = " [ERROR]" if msg.is_error else ""
                click.echo(f"  {ts_str}tool_result{err_flag}: {content_preview}")
            else:
                content_preview = (msg.content or "")[:80].replace("\n", "\\n")
                click.echo(f"  {ts_str}{msg.role}: {content_preview}")


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
@click.option(
    "--fix-timestamps",
    is_flag=True,
    help="Backfill correct timestamps for existing records from their trajectory paths",
)
@click.pass_context
def sync(
    ctx: click.Context,
    harness: str | None,
    since: str,
    with_signals: bool,
    dry_run: bool,
    fix_timestamps: bool,
) -> None:
    """Discover trajectory files and import them into the session store.

    Scans known trajectory directories for gptme, Claude Code, Codex, and
    Copilot sessions and appends a :class:`~gptme_sessions.record.SessionRecord`
    for each one not already in the store.

    Use ``--signals`` to extract productivity signals (outcome, duration,
    deliverables) from each trajectory.  This is slower but produces richer
    records suitable for ``stats`` and ``runs`` analytics.

    Re-running ``sync`` is safe: sessions already in the store (matched by
    trajectory path) are skipped.  With ``--signals``, existing records that
    have ``outcome=unknown`` (no signals yet) will be updated in-place.
    """
    store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])

    # Handle --fix-timestamps: correct timestamps on existing records using
    # session dates extracted from their trajectory paths.
    if fix_timestamps:
        existing_records = store.load_all()
        if not existing_records:
            click.echo("No records in store to fix.")
            return
        fixed = 0
        for rec in existing_records:
            if not rec.trajectory_path:
                continue
            h = rec.harness or "unknown"
            traj = Path(rec.trajectory_path)
            # Prefer the real start time from the trajectory; fall back to
            # session_date at midnight when the trajectory can't be read.
            real_dt = session_datetime_from_path(h, traj) if traj.is_file() else None
            if real_dt:
                correct_ts = real_dt.isoformat()
                new_prefix = real_dt.date().isoformat()
            else:
                sd = session_date_from_path(h, traj)
                if sd is None:
                    continue
                correct_ts = f"{sd.isoformat()}T00:00:00+00:00"
                new_prefix = sd.isoformat()

            # Detect noon-UTC placeholder (all-zero duration + 12:00 timestamp):
            # these are bulk-imported records whose real time is recoverable
            # from the trajectory.
            is_noon_placeholder = (
                real_dt is not None
                and rec.timestamp[11:19] == "12:00:00"
                and (rec.duration_seconds or 0) == 0
            )
            needs_fix = not rec.timestamp.startswith(new_prefix) or is_noon_placeholder
            if needs_fix:
                if dry_run:
                    click.echo(
                        f"  would fix: {rec.session_id}  "
                        f"{rec.timestamp[:19]} → {correct_ts[:19]}"
                    )
                else:
                    rec.timestamp = correct_ts
                fixed += 1
        if not dry_run and fixed:
            store.rewrite(existing_records)
        click.echo(
            f"{'Would fix' if dry_run else 'Fixed'} {fixed} timestamp(s) "
            f"out of {len(existing_records)} record(s)."
        )
        return

    since_days = _parse_since(since) or 14
    discovered = _discover_all(since_days=since_days, harness_filter=harness)

    if not discovered:
        click.echo(f"No sessions found in the last {since_days} day(s).")
        return

    # Build lookup structures for deduplication and in-place updates.
    existing_records = store.load_all()
    existing_by_path = {r.trajectory_path: r for r in existing_records if r.trajectory_path}

    new_records: list[SessionRecord] = []
    updated_paths: set[str] = set()
    imported = 0
    updated = 0
    skipped = 0

    # Warn when a large number of new sessions would be imported.
    # This catches accidental wide-window syncs (e.g. --since 90d) that inflate stats.
    new_count_estimate = sum(1 for e in discovered if str(e["path"]) not in existing_by_path)
    if not dry_run and new_count_estimate > 100:
        click.echo(
            f"Warning: {new_count_estimate} new session(s) would be imported "
            f"(window: {since_days}d). Use --dry-run to preview. Proceeding...",
            err=True,
        )

    for entry in discovered:
        path_str = str(entry["path"])
        traj_path = entry["path"]

        if path_str in existing_by_path:
            existing = existing_by_path[path_str]
            needs_update = False

            # Update model if it was previously unknown and we now know it.
            entry_model = entry.get("model")
            if entry_model and (not existing.model or existing.model == "unknown"):
                existing.model = entry_model
                needs_update = True

            # Backfill session_name and project if missing.
            if not existing.session_name and entry.get("session_name"):
                existing.session_name = entry["session_name"]
                needs_update = True
            if not existing.project and entry.get("project"):
                existing.project = entry["project"]
                needs_update = True

            # With --signals, backfill records that have no outcome yet.
            if with_signals and existing.outcome == "unknown" and traj_path.is_file():
                if not dry_run:
                    try:
                        result = extract_from_path(traj_path)
                        existing.outcome = "productive" if result.get("productive") else "noop"
                        existing.duration_seconds = int(result.get("session_duration_s") or 0)
                        if not existing.deliverables:
                            existing.deliverables = result.get("deliverables", [])
                        if result.get("inferred_category") and not existing.category:
                            existing.category = result["inferred_category"]
                        needs_update = True
                    except Exception as exc:
                        click.echo(
                            f"  warning: signals extraction failed for {path_str}: {exc}",
                            err=True,
                        )
                        if not needs_update:  # don't double-count if model was already updated
                            skipped += 1
                else:
                    needs_update = True  # mark for dry-run reporting
            elif with_signals and existing.outcome == "unknown" and not traj_path.is_file():
                click.echo(
                    f"  warning: trajectory not found, cannot backfill signals for {path_str}",
                    err=True,
                )
                if not needs_update:
                    skipped += 1
            elif not needs_update:
                skipped += 1

            if needs_update:
                if dry_run:
                    click.echo(f"  would update: {entry['harness']:14s}  {path_str}")
                    updated_paths.add(path_str)
                else:
                    updated_paths.add(path_str)
                    updated += 1
            continue

        # Build the record with correct timestamp from the trajectory's first
        # event (not now()).  Without this, all bulk-synced records either get
        # today's timestamp (skewing daily stats) or a noon-UTC placeholder
        # (which collapses many sessions to a single hour and breaks hourly
        # analytics).  Read the real first-event datetime when possible;
        # fall back to noon-UTC on the session_date only if the trajectory
        # has no readable timestamp.
        session_dt: datetime | None = (
            session_datetime_from_path(entry["harness"], traj_path) if traj_path.is_file() else None
        )
        session_date: date | None = entry.get("session_date")
        record_kwargs: dict = {
            "harness": entry["harness"],
            "trajectory_path": path_str,  # used for deduplication on re-sync
        }
        if session_dt:
            record_kwargs["timestamp"] = session_dt.isoformat()
        elif session_date:
            record_kwargs["timestamp"] = datetime(
                session_date.year,
                session_date.month,
                session_date.day,
                12,
                0,
                0,
                tzinfo=timezone.utc,
            ).isoformat()
        if entry.get("model"):
            record_kwargs["model"] = entry["model"]
        if entry.get("session_name"):
            record_kwargs["session_name"] = entry["session_name"]
        if entry.get("project"):
            record_kwargs["project"] = entry["project"]

        if with_signals and traj_path.is_file() and not dry_run:
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
            new_records.append(SessionRecord(**record_kwargs))
            imported += 1

    if not dry_run:
        if updated_paths:
            # Rewrite the store to persist in-place mutations on existing_records.
            # NOTE: existing_records was loaded once at the start of sync; any records
            # appended to the store by a concurrent process after that load may be lost
            # here.  See store.rewrite() docstring for details on this known trade-off.
            store.rewrite(existing_records + new_records)
        else:
            for rec in new_records:
                store.append(rec)

    if dry_run:
        n_would_update = len(updated_paths)
        n_would_import = len(discovered) - skipped - n_would_update
        click.echo(
            f"\n{len(discovered)} found, {skipped} unchanged, "
            f"{n_would_import} would be imported"
            + (f", {n_would_update} would be updated" if n_would_update else "")
        )
    else:
        parts = [f"Imported {imported} session(s)"]
        if updated:
            parts.append(f"updated {updated}")
        parts.append(f"{skipped} unchanged.")
        click.echo(", ".join(parts))


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
@click.option(
    "--context-tier",
    default=None,
    type=click.Choice(sorted(VALID_CONTEXT_TIERS)),
    help="Context tier used in this session (standard, extended, large, massive)",
)
@click.option(
    "--ab-group",
    default=None,
    type=click.Choice(sorted(VALID_AB_GROUPS)),
    help="A/B experiment group (control or treatment)",
)
@click.option("--tier-version", default=None, help="Context tier config version for this session")
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
    context_tier: str | None,
    ab_group: str | None,
    tier_version: str | None,
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
        context_tier=context_tier,
        ab_group=ab_group,
        tier_version=tier_version,
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
    from .judge import DEFAULT_GOALS, judge_session

    if dry_run and update_store:
        raise click.UsageError("--dry-run and --update-store are mutually exclusive")

    if journal_dir is None:
        journal_dir = Path.cwd() / "journal"
    if not journal_dir.is_dir():
        raise click.BadParameter(f"{journal_dir} is not a directory", param_hint="'--journal-dir'")

    # Discover autonomous session entries
    entries: list[Path] = []
    for day_dir in sorted(journal_dir.iterdir()):
        if day_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            entries.extend(sorted(day_dir.glob("autonomous-session-*.md")))
    if last <= 0:
        raise click.UsageError("--last must be a positive integer (got 0 or negative)")
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
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Skipping %s: %s", entry, e)
            continue

        try:
            meta = parse_meta(text)
            sid = extract_sid(entry)
            cat = meta.get("category", "unknown")
            outcome = meta.get("outcome", "unknown")
            entry_date = entry.parent.name

            result_row: dict = {
                "session_id": sid,
                "date": entry_date,
                "category": cat,
                "outcome": outcome,
                "journal_path": str(entry),
            }

            if dry_run:
                results.append(result_row)
                if not as_json:
                    click.echo(f"  {sid:<12} {entry_date}  {cat:<14} {outcome}")
                continue

            verdict = judge_session(text, category=cat, goals=effective_goals)
            score = verdict["score"] if verdict else None
            reason = verdict["reason"] if verdict else None

            result_row["llm_judge_score"] = score
            result_row["llm_judge_reason"] = reason
            result_row["llm_judge_model"] = verdict["model"] if verdict else None
            results.append(result_row)

            if not as_json and score is not None:
                click.echo(f"  {sid:<12} {entry_date}  {cat:<14} {score:.2f}  {reason}")
        except Exception as e:
            logger.warning("Error processing %s: %s", entry, e)
            continue

    if dry_run:
        if as_json:
            click.echo(json.dumps(results, indent=2))
        else:
            click.echo(f"\n{len(results)} session(s) (dry run)")
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
                rec.llm_judge_model = s["llm_judge_model"]
                updated += 1
        if updated:
            store.rewrite(records)
            click.echo(f"\nUpdated {updated} record(s) in {store.path}", err=True)
        elif not score_map:
            click.echo(
                "\nNo sessions were scored — check ANTHROPIC_API_KEY and the anthropic package.",
                err=True,
            )
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
        else:
            click.echo(
                "\nNo sessions scored. Check that ANTHROPIC_API_KEY is set"
                " and the anthropic package is installed.",
                err=True,
            )


@cli.command()
@click.option(
    "--journal-dir",
    type=click.Path(path_type=Path, exists=True),  # type: ignore[type-var]
    default=None,
    help="Path to journal directory (default: ./journal)",
)
@click.option("--last", type=int, default=20, help="Classify last N sessions (default: 20)")
@click.option(
    "--llm/--no-llm", default=True, help="Use LLM classifier (default: yes, keyword fallback)"
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--update-store",
    is_flag=True,
    help="Write categories back to session-records.jsonl (matching by session_id)",
)
@click.option(
    "--judge",
    "also_judge",
    is_flag=True,
    help="Also score goal-alignment in same LLM call (requires --llm)",
)
@click.option("--goals", default=None, help="Agent goals for judge scoring (with --judge)")
@click.pass_context
def classify(
    ctx: click.Context,
    journal_dir: Path | None,
    last: int,
    llm: bool,
    as_json: bool,
    update_store: bool,
    also_judge: bool,
    goals: str | None,
) -> None:
    """Classify sessions by work category (code, infrastructure, triage, etc.).

    Reads autonomous session journal entries and classifies each into a
    work category. Uses LLM classification by default with keyword fallback.

    With --judge, also scores goal-alignment in the same LLM call (cheaper
    than separate classify + judge calls).
    """
    from .classification import (
        classify_by_keywords,
        classify_by_llm,
        judge_and_classify,
    )

    if also_judge and not llm:
        raise click.UsageError("--judge requires --llm (cannot judge without LLM)")

    if journal_dir is None:
        journal_dir = Path.cwd() / "journal"
    if not journal_dir.is_dir():
        raise click.BadParameter(f"{journal_dir} is not a directory", param_hint="'--journal-dir'")

    # Discover autonomous session entries
    entries: list[Path] = []
    for day_dir in sorted(journal_dir.iterdir()):
        if day_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            entries.extend(sorted(day_dir.glob("autonomous-session-*.md")))
    if last <= 0:
        raise click.UsageError("--last must be a positive integer")
    entries = entries[-last:]

    if not entries:
        click.echo("No autonomous session journal entries found.", err=True)
        return

    click.echo(f"Classifying {len(entries)} session(s)...", err=True)

    def extract_sid(p: Path) -> str:
        m = re.match(r"autonomous-session-(\w+)", p.stem)
        return m.group(1) if m else p.stem

    results: list[dict] = []
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Skipping %s: %s", entry, e)
            continue

        sid = extract_sid(entry)
        entry_date = entry.parent.name

        try:
            if also_judge:
                classification, judge_result = judge_and_classify(text, goals=goals)
                if classification is None:
                    logger.warning(
                        "judge_and_classify failed for %s; falling back to keyword classification",
                        sid,
                    )
                    classification = classify_by_keywords(text)
            elif llm:
                classification = classify_by_llm(text)
                if classification is None:
                    classification = classify_by_keywords(text)
                judge_result = None
            else:
                classification = classify_by_keywords(text)
                judge_result = None

            row: dict = {
                "session_id": sid,
                "date": entry_date,
                **classification.to_dict(),
                "journal_path": str(entry),
            }
            if judge_result:
                row["llm_judge_score"] = judge_result["score"]
                row["llm_judge_reason"] = judge_result["reason"]
                row["llm_judge_model"] = judge_result["model"]

            results.append(row)

            if not as_json:
                score_str = f"  {judge_result['score']:.2f}" if judge_result else ""
                click.echo(
                    f"  {sid:<12} {entry_date}  {classification.category:<16} "
                    f"conf={classification.confidence:.2f}  "
                    f"[{classification.classifier}]{score_str}"
                )
        except Exception as e:
            logger.warning("Error processing %s: %s", entry, e)
            continue

    # Write categories back to store if requested
    if update_store:
        store = SessionStore(sessions_dir=ctx.obj["sessions_dir"])
        records = store.load_all()
        cat_map = {r["session_id"]: r for r in results}
        updated = 0
        for rec in records:
            if rec.session_id in cat_map:
                r = cat_map[rec.session_id]
                rec.category = r["category"]
                if r.get("llm_judge_score") is not None:
                    rec.llm_judge_score = r["llm_judge_score"]
                    rec.llm_judge_reason = r["llm_judge_reason"]
                    rec.llm_judge_model = r["llm_judge_model"]
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
        from collections import Counter

        cats = Counter(r["category"] for r in results)
        productive = sum(1 for r in results if r.get("productive"))
        click.echo(f"\nClassified: {len(results)} sessions  ({productive} productive)")
        for cat, count in cats.most_common():
            click.echo(f"  {cat:<16} {count}")


def _discover_journal_entries(
    journal_dir: Path,
    last: int,
) -> list[Path]:
    """Discover autonomous session journal entries, sorted chronologically."""
    entries: list[Path] = []
    for day_dir in sorted(journal_dir.iterdir()):
        if day_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            entries.extend(sorted(day_dir.glob("*autonomous-session-*.md")))
    return entries[-last:] if last > 0 else entries


@cli.command("classify-stats")
@click.option(
    "--journal-dir",
    type=click.Path(path_type=Path, exists=True),  # type: ignore[type-var]
    default=None,
    help="Path to journal directory (default: ./journal)",
)
@click.option("--last", type=int, default=20, help="Number of sessions to analyze (default: 20)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--diversity-window", type=int, default=5, help="Window for diversity check (default: 5)"
)
def classify_stats(
    journal_dir: Path | None,
    last: int,
    as_json: bool,
    diversity_window: int,
) -> None:
    """Show classification stats and session diversity alerts.

    Classifies recent sessions using the fast keyword classifier and shows
    category breakdown, productivity rate, trends, and diversity warnings.
    """
    from collections import Counter

    from .classification import classify_by_keywords

    if journal_dir is None:
        journal_dir = Path.cwd() / "journal"
    if not journal_dir.is_dir():
        raise click.BadParameter(f"{journal_dir} is not a directory", param_hint="'--journal-dir'")

    entries = _discover_journal_entries(journal_dir, last)
    if not entries:
        click.echo("No autonomous session journal entries found.", err=True)
        return

    # Classify all entries (keyword-only for speed)
    results: list[dict] = []
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        result = classify_by_keywords(text)
        row = {
            "date": entry.parent.name,
            **result.to_dict(),
        }
        results.append(row)

    if not results:
        click.echo("No sessions could be classified.", err=True)
        return

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    total = len(results)
    productive = sum(1 for r in results if r.get("productive"))
    cats = Counter(r["category"] for r in results)

    # Multi-label counts (primary + secondary)
    cats_multi: Counter[str] = Counter()
    for r in results:
        cats_multi[r["category"]] += 1
        sec = r.get("secondary_category")
        if sec:
            cats_multi[sec] += 1

    click.echo(f"Session Classification Stats (last {total} sessions)")
    click.echo("=" * 50)
    click.echo(f"Productive: {productive}/{total} ({productive * 100 // total}%)")
    click.echo(f"NOOP:       {total - productive}/{total} ({(total - productive) * 100 // total}%)")
    click.echo()
    click.echo("Category Breakdown (primary):")
    for cat, count in cats.most_common():
        pct = count * 100 // total
        bar = "#" * (pct // 2)
        click.echo(f"  {cat:15s} {count:3d} ({pct:2d}%) {bar}")

    # Multi-label view
    has_secondary = any(r.get("secondary_category") for r in results)
    if has_secondary:
        click.echo()
        click.echo("Category Presence (primary + secondary):")
        for cat, count in cats_multi.most_common():
            pct = count * 100 // total
            bar = "#" * (pct // 2)
            primary_count = cats.get(cat, 0)
            secondary_count = count - primary_count
            detail = f"{primary_count}p"
            if secondary_count > 0:
                detail += f"+{secondary_count}s"
            click.echo(f"  {cat:15s} {count:3d} ({pct:2d}%) {bar}  [{detail}]")

    # Missing categories
    productive_cats = {
        "code",
        "infrastructure",
        "triage",
        "strategic",
        "content",
        "cross-repo",
        "research",
    }
    present_cats = set(cats_multi.keys())
    missing = productive_cats - present_cats
    if missing:
        click.echo(f"\n  Missing categories: {', '.join(sorted(missing))}")

    # Trend (last 5 vs previous 5)
    if total >= 10:
        recent = results[-5:]
        earlier = results[-10:-5]
        recent_prod = sum(1 for c in recent if c.get("productive"))
        earlier_prod = sum(1 for c in earlier if c.get("productive"))
        if recent_prod > earlier_prod:
            trend = "improving"
        elif recent_prod < earlier_prod:
            trend = "declining"
        else:
            trend = "stable"
        click.echo(f"\nTrend: {trend} (recent 5: {recent_prod}/5, previous 5: {earlier_prod}/5)")

    # Diversity check
    if len(results) >= diversity_window:
        click.echo()
        click.echo(f"Session Diversity (last {diversity_window}):")
        recent_cats = [r["category"] for r in results[-diversity_window:]]
        alerts: list[str] = []

        if len(recent_cats) >= 3 and len(set(recent_cats[-3:])) == 1:
            alerts.append(f"3+ consecutive '{recent_cats[-1]}' sessions — consider diversifying")

        non_code = sum(1 for c in recent_cats if c in ("triage", "monitoring"))
        if non_code >= 3:
            alerts.append(
                f"{non_code}/{diversity_window} sessions were triage/monitoring — pivot to code or ideas"
            )

        code_sessions = sum(1 for c in recent_cats if c == "code")
        if code_sessions == 0 and diversity_window >= 5:
            alerts.append("No code sessions in last 5 — consider picking up a coding task")

        if alerts:
            for alert in alerts:
                click.echo(f"  ⚠️  {alert}")
        else:
            unique = len(set(recent_cats))
            cats_str = ", ".join(f"{c}({recent_cats.count(c)})" for c in dict.fromkeys(recent_cats))
            click.echo(f"  ✅ Good diversity: {unique}/{diversity_window} categories — {cats_str}")


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
