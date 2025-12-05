"""Command-line interface for run loops."""

import sys
from pathlib import Path

import click

from run_loops.autonomous import AutonomousRun
from run_loops.email import EmailRun
from run_loops.project_monitoring import ProjectMonitoringRun


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
    "--org",
    default="gptme",
    help="GitHub organization to monitor",
)
@click.option(
    "--author",
    default="TimeToBuildBob",
    help="GitHub username for filtering",
)
def monitoring(workspace: Path, org: str, author: str):
    """Run project monitoring loop."""
    run = ProjectMonitoringRun(workspace, target_org=org, author=author)
    exit_code = run.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
