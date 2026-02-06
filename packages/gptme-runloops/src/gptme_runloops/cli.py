"""Command-line interface for run loops."""

import os
import sys
from pathlib import Path

import click

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.email import EmailRun
from gptme_runloops.project_monitoring import ProjectMonitoringRun
from gptme_runloops.team import TeamRun


@click.group()
def main():
    """Run loop framework for autonomous AI agent operation."""
    pass


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
def autonomous(workspace: Path):
    """Run autonomous operation loop."""
    run = AutonomousRun(workspace)
    exit_code = run.run()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Workspace directory (default: current directory)",
)
def email(workspace: Path):
    """Run email processing loop."""
    run = EmailRun(workspace)
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
def team(workspace: Path, tools: str | None):
    """Run autonomous team coordination loop.

    The coordinator agent runs with restricted tools and delegates
    all work to subagents via gptodo. Inspired by Claude Code Agent Teams.
    """
    run = TeamRun(workspace, tools=tools)
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
def monitoring(
    workspace: Path,
    orgs: tuple[str, ...],
    repos: tuple[str, ...],
    author: str,
    agent_name: str,
):
    """Run project monitoring loop."""
    run = ProjectMonitoringRun(
        workspace,
        target_orgs=list(orgs) if orgs else None,
        target_repos=list(repos) if repos else None,
        author=author,
        agent_name=agent_name,
    )
    exit_code = run.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
