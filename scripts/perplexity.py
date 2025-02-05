#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click",
#     "httpx",
#     "rich",
#     "openai>=1.57.0",
#     "tabulate",
#     "python-dotenv",
# ]
# ///
import json
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

load_dotenv()

# Get logger but don't configure it (will be configured by CLI)
logger = logging.getLogger("perplexity")

console = Console()


@dataclass
class SearchResult:
    """Represents a search result from Perplexity"""

    answer: str
    sources: list[str]
    query: str


class PerplexitySearch:
    """Handles searching with Perplexity's API"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._get_api_key()
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://api.perplexity.ai",
        )

    def _get_api_key(self) -> str:
        """Get API key from environment or config file"""

        # Try environment variable first
        if api_key := os.getenv("PERPLEXITY_API_KEY"):
            return api_key

        # Try config file
        config_path = Path.home() / ".config" / "gptme" / "config.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
                if api_key := config.get("env", {}).get("PERPLEXITY_API_KEY"):
                    return api_key  # type: ignore

        raise ValueError(
            f"Perplexity API key not found. Set PERPLEXITY_API_KEY environment variable or add 'PERPLEXITY_API_KEY' to the env section in {config_path}"
        )

    def search(self, query: str, mode: str = "concise") -> SearchResult:
        """
        Search using Perplexity API

        Args:
            query: Search query
            mode: Search mode ('concise' or 'copilot')
        """
        with Live(Spinner("runner", "Researching query..."), refresh_per_second=10):
            response = self.client.chat.completions.create(
                model="llama-3.1-sonar-large-128k-online",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an artificial intelligence assistant and you need to engage in a helpful, detailed, polite conversation with a user.",
                    },
                    {
                        "role": "user",
                        "content": query,
                    },
                ],
            )
        msg = response.choices[0].message
        assert msg.content

        return SearchResult(
            answer=msg.content,
            sources=[],
            query=query,
        )


@click.group()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
def cli(verbose: bool) -> None:
    """Search the web using Perplexity AI"""
    # Configure logging based on verbosity
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )


@cli.command()
@click.argument("query")
@click.option(
    "--mode",
    type=click.Choice(["concise", "copilot"]),
    default="concise",
    help="Search mode",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Output raw JSON instead of formatted text",
)
def search(query: str, mode: str, raw: bool) -> None:
    """Search using Perplexity AI"""
    perplexity = PerplexitySearch()
    result = perplexity.search(query, mode=mode)

    if raw:
        click.echo(
            json.dumps(
                {
                    "answer": result.answer,
                    "sources": result.sources,
                    "query": result.query,
                },
                indent=2,
            )
        )
    else:
        # Print answer as plain text
        console.print(result.answer)

        # Print sources if available
        if result.sources:
            console.print("\nSources:", style="bold")
            for source in result.sources:
                console.print(f"- {source}")


if __name__ == "__main__":
    cli()
