#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "tweepy>=4.14.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "click>=8.0.0",
#   "pyyaml>=6.0.0",
#   "schedule>=1.2.0",
#   "python-crontab>=2.8.0",
#   "flask>=3.0.0",
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
# ]
# ///
"""
Twitter CLI - Command-line interface for Twitter automation.

This script provides a unified interface to:
- Basic Twitter operations (post, read, monitor)
- Workflow management (drafts, review, scheduling)
- Automation setup (cron jobs)

Usage:
    ./twitter-cli.py twitter post "Hello world!"     # Post a tweet
    ./twitter-cli.py twitter timeline                # Read timeline
    ./twitter-cli.py monitor                 # Start monitoring
    ./twitter-cli.py workflow review         # Review drafts
    ./twitter-cli.py cron install           # Setup automation
"""

import click
from gptme.init import init as init_gptme
from rich.console import Console

from twitter.setup_cron import install as install_cron
from twitter.setup_cron import remove as remove_cron
from twitter.setup_cron import status as cron_status
from twitter.twitter import cli as twitter_cli
from twitter.workflow import cli as workflow_cli

console = Console()


@click.group()
@click.option(
    "--model",
    default="anthropic/claude-3-5-sonnet-20241022",
    help="Model to use for LLM operations",
)
def cli(model: str):
    """Twitter automation CLI"""

    # Initialize gptme with no tools
    init_gptme(model=model, interactive=False, tool_allowlist=frozenset())


# Import and add the twitter CLI commands as a subgroup
cli.add_command(twitter_cli, name="twitter")

# Import and add workflow CLI commands as a subgroup
cli.add_command(workflow_cli, name="workflow")


# Automation setup
@cli.group()
def cron():
    """Manage automation"""
    pass


@cron.command()
@click.option("--monitor-interval", default=15, help="Monitor interval in minutes (5-60)")
@click.option(
    "--review-time",
    default="9,13,17",
    help="Daily review notification times (24h format)",
)
@click.option("--post-interval", default=30, help="Post check interval in minutes (15-60)")
def install(monitor_interval: int, review_time: str, post_interval: int):
    """Install cron jobs"""

    install_cron(
        monitor_interval=monitor_interval,
        review_time=review_time,
        post_interval=post_interval,
    )


@cron.command()
def remove():
    """Remove cron jobs"""

    remove_cron()


@cron.command()
def status():
    """Show cron job status"""

    cron_status()


if __name__ == "__main__":
    cli()
