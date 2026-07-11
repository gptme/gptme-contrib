"""Command-line interface for run loops."""

import os
import sys
from pathlib import Path

import click

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.email import EmailRun
from gptme_runloops.project_monitoring import ProjectMonitoringRun
from gptme_runloops.team import TeamRun
from gptme_runloops.utils.executor import get_executor, list_backends


@click.group()
def main():
    """Run loop framework for autonomous AI agent operation."""
    pass


def _backend_option(f=None, *, default: str = "gptme", note: str | None = None):
    """Shared --backend option for all commands."""
    backends = list_backends()
    help_text = f"Execution backend (available: {', '.join(backends)})"
    if note:
        help_text = f"{help_text}. {note}"

    option = click.option(
        "--backend",
        default=default,
        type=click.Choice(backends),
        help=help_text,
    )
    return option(f) if f is not None else option


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def autonomous(
    workspace: Path, model: str | None, tool_format: str | None, backend: str
):
    """Run autonomous operation loop."""
    executor = get_executor(backend)
    run = AutonomousRun(
        workspace, model=model, tool_format=tool_format, executor=executor
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def email(workspace: Path, model: str | None, tool_format: str | None, backend: str):
    """Run email processing loop."""
    executor = get_executor(backend)
    run = EmailRun(workspace, model=model, tool_format=tool_format, executor=executor)
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--tools",
    default=None,
    help="Override coordinator tools (default: gptodo,save,append,...)",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option
def team(
    workspace: Path,
    tools: str | None,
    model: str | None,
    tool_format: str | None,
    backend: str,
):
    """Run autonomous team coordination loop.

    The coordinator agent runs with restricted tools and delegates
    all work to subagents via gptodo. Inspired by Claude Code Agent Teams.
    """
    executor = get_executor(backend)
    run = TeamRun(
        workspace,
        tools=tools,
        model=model,
        tool_format=tool_format,
        executor=executor,
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
@click.option(
    "--org",
    "orgs",
    multiple=True,
    help="GitHub organization(s) to monitor (can be specified multiple times)",
)
@click.option(
    "--repo",
    "repos",
    multiple=True,
    help="Specific repository to monitor in owner/repo format (can be specified multiple times)",
)
@click.option(
    "--author",
    default=os.environ.get("GITHUB_AUTHOR", ""),
    help="GitHub username for filtering (default: $GITHUB_AUTHOR env var)",
)
@click.option(
    "--agent-name",
    default=os.environ.get("AGENT_NAME", "Agent"),
    help="Agent name for prompts (default: $AGENT_NAME env var or 'Agent')",
)
@click.option(
    "--model",
    default=None,
    help="Model override (e.g. 'openai-subscription/gpt-5.3-codex')",
)
@click.option(
    "--tool-format",
    default=None,
    type=click.Choice(["markdown", "xml", "tool"]),
    help="Tool format override",
)
@_backend_option(
    default="claude-code",
    note=(
        "Monitoring defaults to claude-code because gptme+slow-models is "
        "100% NOOP on monitoring (82/82 sessions over 3 days — produces "
        "analysis but zero concrete actions)"
    ),
)
def monitoring(
    workspace: Path,
    orgs: tuple[str, ...],
    repos: tuple[str, ...],
    author: str,
    agent_name: str,
    model: str | None,
    tool_format: str | None,
    backend: str,
):
    """Run project monitoring loop.

    Monitoring defaults to claude-code (not gptme) because the gptme
    harness with slower/larger models historically produced 100% NOOP
    sessions — verbose analysis output with zero commits or tool actions.
    gptme remains available as an explicit --backend gptme override for
    testing or quota-diversion scenarios.
    """
    executor = get_executor(backend)
    run = ProjectMonitoringRun(
        workspace,
        target_orgs=list(orgs) if orgs else None,
        target_repos=list(repos) if repos else None,
        author=author,
        agent_name=agent_name,
        model=model,
        tool_format=tool_format,
        executor=executor,
    )
    exit_code = run.run()
    sys.exit(exit_code)


@main.command("run-item")
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Agent workspace repo root",
)
@click.option(
    "--work-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Grouped-item JSONL work file (default: $PM_WORK_FILE)",
)
@click.option(
    "--backend",
    default=None,
    help="Session backend, passed to the runner verbatim (default: $BOB_BACKEND). "
    "Routing stays in the dispatcher — run-item never re-routes.",
)
@click.option(
    "--model",
    default=None,
    help="Model override (default: $BOB_SELECTED_MODEL; empty = backend default)",
)
@click.option(
    "--lane",
    default=None,
    help="Dispatch lane for ledger correlation (default: $PM_LANE, else mixed)",
)
@click.option(
    "--dispatch-id",
    default=None,
    help="Dispatch/unit id for ledger correlation (default: $PM_DISPATCH_ID)",
)
@click.option(
    "--slot-key",
    default=None,
    help="Slot key — selects the per-slot lockfile (default: $PM_SLOT_KEY)",
)
@click.option(
    "--author",
    default=None,
    help="GitHub author login (default: $GITHUB_AUTHOR)",
)
@click.option(
    "--agent-name",
    default=None,
    help="Agent display name for prompts (default: $AGENT_NAME)",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Policy TOML (default: <workspace>/config/pm-run-item.toml if present)",
)
@click.option(
    "--claim-mode",
    type=click.Choice(["acquire", "preheld", "none"]),
    default="acquire",
    help="Coordination-claim handling (preheld is reserved for the "
    "dispatcher-held-claim migration step)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve decisions + render prompts + print the ExecutionPlan JSON; "
    "execute nothing (read-only gate/status probes still run)",
)
def run_item_cmd(
    workspace: Path,
    work_file: Path | None,
    backend: str | None,
    model: str | None,
    lane: str | None,
    dispatch_id: str | None,
    slot_key: str | None,
    author: str | None,
    agent_name: str | None,
    config_path: Path | None,
    claim_mode: str,
    dry_run: bool,
):
    """Execute ONE PM work-file end-to-end (the uniform executor surface).

    Reads a grouped work item (the slot JSONL shape pm_dispatch emits),
    resolves the action via the merge-lifecycle decisions, renders the
    prompt via the prompt templates, executes the session via the runner
    (run.sh), and records outcomes via worker_records — one uniform call
    replacing the PM_DETACHED bash re-exec path.
    """
    import logging
    import signal

    from gptme_runloops.run_item import build_execution_plan, run_work_file
    from gptme_runloops.run_item_config import assemble_hooks, load_run_item_config

    # Progress lines (the bash-echo mirror) go to stderr so --dry-run stdout
    # stays pure ExecutionPlan JSON; under a slot unit both streams hit the
    # journal.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    run_item_logger = logging.getLogger("gptme_runloops.run_item")
    run_item_logger.addHandler(handler)
    run_item_logger.setLevel(logging.INFO)

    # EXIT-trap parity: systemd RuntimeMaxSec sends SIGTERM; convert it to
    # SystemExit so try/finally (claim abandon, lock release, record write
    # cleanup) still runs on timeout-killed slots.
    def _sigterm(_signum, _frame):  # pragma: no cover - signal path
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _sigterm)

    work_file = work_file or (
        Path(os.environ["PM_WORK_FILE"]) if os.environ.get("PM_WORK_FILE") else None
    )
    if work_file is None:
        raise click.UsageError("--work-file is required (or set $PM_WORK_FILE)")
    backend = backend or os.environ.get("BOB_BACKEND", "")
    if not backend:
        raise click.UsageError("--backend is required (or set $BOB_BACKEND)")
    model = model if model is not None else os.environ.get("BOB_SELECTED_MODEL", "")
    lane = lane or os.environ.get("PM_LANE", "mixed")
    dispatch_id = dispatch_id or os.environ.get("PM_DISPATCH_ID", "")
    slot_key = slot_key or os.environ.get("PM_SLOT_KEY", "")

    overrides: dict[str, str] = {}
    if author or os.environ.get("GITHUB_AUTHOR"):
        overrides["author"] = author or os.environ.get("GITHUB_AUTHOR", "")
    if agent_name or os.environ.get("AGENT_NAME"):
        overrides["agent_name"] = agent_name or os.environ.get("AGENT_NAME", "")

    config, raw = load_run_item_config(workspace, config_path, **overrides)
    hooks = assemble_hooks(config, raw)

    if dry_run:
        plan = build_execution_plan(
            work_file,
            config,
            hooks,
            backend=backend,
            model=model,
            lane=lane,
            dispatch_id=dispatch_id,
            slot_key=slot_key,
            claim_mode=claim_mode,
        )
        click.echo(plan.to_json())
        sys.exit(0)

    exit_code = run_work_file(
        work_file,
        config,
        hooks,
        backend=backend,
        model=model,
        lane=lane,
        dispatch_id=dispatch_id,
        slot_key=slot_key,
        claim_mode=claim_mode,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
