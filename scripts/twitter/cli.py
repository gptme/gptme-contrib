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
#   "flask>=3.0.0",
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
# ]
# ///
"""
Twitter CLI - Command-line interface for Twitter automation.

This script provides a unified interface to:
- Basic Twitter operations (post, read, monitor)
- Workflow management (drafts, review, scheduling)

Usage:
    ./cli.py twitter post "Hello world!"     # Post a tweet
    ./cli.py twitter timeline                # Read timeline
    ./cli.py workflow monitor                # Start monitoring
    ./cli.py workflow review                 # Review drafts
"""

import click
from gptme.init import init as init_gptme
from rich.console import Console

from .twitter import cli as twitter_cli
from .workflow import cli as workflow_cli

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


if __name__ == "__main__":
    cli()
