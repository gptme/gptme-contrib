"""Command-line interface for run loops."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import click

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.email import EmailRun
from gptme_runloops.project_monitoring import ProjectMonitoringRun
from gptme_runloops.run_item import (
    RunItemConfig,
    RunItemHooks,
    execute_plan,
    load_items,
    plan_run_item,
)
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


@main.command(name="run-item")
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Workspace containing run.sh and PM policy hooks.",
)
@click.option(
    "--work-file",
    type=click.Path(path_type=Path),
    default=lambda: os.environ.get("PM_WORK_FILE"),
    required=True,
    help="Grouped JSONL work file (default: $PM_WORK_FILE).",
)
@click.option("--backend", default=lambda: os.environ.get("BOB_BACKEND", "claude-code"))
@click.option("--model", default=lambda: os.environ.get("BOB_SELECTED_MODEL") or None)
@click.option(
    "--lane",
    type=click.Choice(["fast", "slow", "mixed"]),
    default=lambda: os.environ.get("PM_LANE", "mixed"),
)
@click.option("--dispatch-id", default=lambda: os.environ.get("PM_DISPATCH_ID") or None)
@click.option("--author", default=lambda: os.environ.get("GITHUB_AUTHOR", ""))
@click.option("--agent-name", default=lambda: os.environ.get("AGENT_NAME", "Agent"))
@click.option(
    "--claim-mode", type=click.Choice(["acquire", "preheld", "none"]), default="acquire"
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Emit canonical execution-plan JSON and perform no work.",
)
def run_item(
    workspace: Path,
    work_file: Path,
    backend: str,
    model: str | None,
    lane: str,
    dispatch_id: str | None,
    author: str,
    agent_name: str,
    claim_mode: str,
    dry_run: bool,
):
    """Execute one grouped PM work file through the shared run.sh runner."""
    config = RunItemConfig(
        workspace=workspace,
        backend=backend,
        model=model,
        lane=lane,
        dispatch_id=dispatch_id,
        author=author,
        agent_name=agent_name,
        run_salt=os.environ.get("RUN_START", ""),
        claim_mode=claim_mode,
    )
    hooks = RunItemHooks.from_workspace(workspace)
    rules = ""
    if hooks.monitoring_rules_file and hooks.monitoring_rules_file.is_file():
        rules = hooks.monitoring_rules_file.read_text(encoding="utf-8")
    try:
        items = load_items(work_file)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    plans = [
        plan_run_item(item, config, index=index, monitoring_rules=rules)
        for index, item in enumerate(items, 1)
    ]
    if dry_run:
        click.echo(
            json.dumps(
                [json.loads(plan.to_json()) for plan in plans],
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        return
    exit_code = 0
    for item, plan in zip(items, plans, strict=True):
        item_hooks = hooks
        if claim_mode == "acquire":
            owner = f"project-monitoring-{backend}-{plan.session_id}"

            def claim(key: str, *, _owner: str = owner) -> bool:
                return (
                    subprocess.run(
                        [
                            "uv",
                            "run",
                            "coordination",
                            "work-claim",
                            _owner,
                            key,
                            "--ttl",
                            "60",
                        ],
                        cwd=workspace,
                        check=False,
                        stdout=subprocess.DEVNULL,
                    ).returncode
                    == 0
                )

            def abandon(key: str, *, _owner: str = owner) -> None:
                subprocess.run(
                    ["uv", "run", "coordination", "work-abandon", _owner, key],
                    cwd=workspace,
                    check=False,
                    stdout=subprocess.DEVNULL,
                )

            item_hooks = replace(hooks, claim=claim, abandon=abandon)
        outcome = execute_plan(plan, item, item_hooks)
        exit_code = outcome.exit_code or exit_code
        if outcome.rate_limited:
            break
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
