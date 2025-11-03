#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click",
#     "exa-py>=0.7.0",
#     "rich",
#     "tabulate",
#     "python-dotenv",
# ]
# ///
import json
import logging
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict

import click
from dotenv import load_dotenv
from exa_py import Exa
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

load_dotenv()

# Get logger but don't configure it (will be configured by CLI)
logger = logging.getLogger("exa")

console = Console()


@dataclass
class SearchResult:
    """Represents a search result from Exa"""

    answer: str
    sources: List[Dict[str, str]]
    query: str


class ExaSearch:
    """Handles searching with Exa's API"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._get_api_key()
        self.client = Exa(api_key=self.api_key)

    def _get_api_key(self) -> str:
        """Get API key from environment or config file"""

        # Try environment variable first
        if api_key := os.getenv("EXA_API_KEY"):
            return api_key

        # Try config file
        config_path = Path.home() / ".config" / "gptme" / "config.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
                if api_key := config.get("env", {}).get("EXA_API_KEY"):
                    return api_key  # type: ignore

        raise ValueError(
            f"Exa API key not found. Set EXA_API_KEY environment variable or add 'EXA_API_KEY' to the env section in {config_path}"
        )

    def search(
        self, query: str, text: bool = True, num_results: int = 10
    ) -> SearchResult:
        """
        Search using Exa API

        Args:
            query: Search query
            text: Whether to return text content (True) or HTML (False)
            num_results: Number of search results to return
        """
        with Live(Spinner("dots", "Researching query..."), refresh_per_second=10):
            try:
                # Get search results
                response = self.client.search(
                    query,
                    num_results=num_results,
                    use_autoprompt=True,
                )

                # Process search results for sources
                sources = self._extract_sources(response.results)

                # Get answer from Exa
                answer_response = self.client.answer(
                    query,
                    text=text,
                )

                # Extract answer text from response
                answer_text = self._extract_answer_text(answer_response)

                return SearchResult(
                    answer=answer_text,
                    sources=sources,
                    query=query,
                )
            except Exception as e:
                logger.error(f"Error during Exa search: {e}")
                raise

    def _extract_sources(self, results) -> List[Dict[str, str]]:
        """Extract sources from search results"""
        sources: List[Dict[str, str]] = []
        for result in results:
            source = {
                "title": "No title",
                "url": "No URL",
                "content_snippet": "No content",
            }

            # Safe attribute access
            if hasattr(result, "title"):
                title = getattr(result, "title")
                if isinstance(title, str):
                    source["title"] = title
            if hasattr(result, "url"):
                url = getattr(result, "url")
                if isinstance(url, str):
                    source["url"] = url
            if hasattr(result, "text"):
                text_content = getattr(result, "text")
                if isinstance(text_content, str):
                    source["content_snippet"] = text_content
            elif hasattr(result, "content"):
                content = getattr(result, "content")
                if isinstance(content, str):
                    source["content_snippet"] = content

            sources.append(source)
        return sources

    def _extract_answer_text(self, response) -> str:
        """Extract answer text from response object"""
        # Handle different response types safely
        if hasattr(response, "answer"):
            answer = getattr(response, "answer")
            if isinstance(answer, str):
                return answer
        elif hasattr(response, "text"):
            text_content = getattr(response, "text")
            if isinstance(text_content, str):
                return text_content
        elif isinstance(response, str):
            return response

        # Fallback for any other response type
        return str(response)


@click.group()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
def cli(verbose: bool = False) -> None:
    """Search the web using Exa AI"""
    # Configure logging based on verbosity
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )

    if verbose:
        logger.debug("Debug logging enabled")
        logger.debug(f"Python version: {sys.version}")
        logger.debug(f"Current directory: {os.getcwd()}")


@cli.command()
@click.argument("query")
@click.option(
    "--results",
    type=int,
    default=10,
    help="Number of search results to return",
)
@click.option(
    "--full",
    is_flag=True,
    help="Return full text content instead of snippets",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Output raw JSON instead of formatted text",
)
def search(query: str, results: int, full: bool, raw: bool) -> None:
    """Search using Exa AI"""
    try:
        exa = ExaSearch()
        result = exa.search(query, text=True, num_results=results)

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
                for i, source in enumerate(result.sources, 1):
                    console.print(f"{i}. {source['title']}", style="bold")
                    console.print(f"   {source['url']}", style="italic blue")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        if logging.getLogger().level == logging.DEBUG:
            import traceback

            console.print(traceback.format_exc())
        sys.exit(1)


@cli.command()
@click.argument("query")
@click.option(
    "--results",
    type=int,
    default=10,
    help="Number of search results to return",
)
@click.option(
    "--raw-response",
    is_flag=True,
    help="Return raw API response instead of formatted text",
)
def answer(query: str, results: int, raw_response: bool) -> None:
    """Get a direct answer using Exa AI"""
    exa = ExaSearch()

    try:
        with Live(Spinner("dots", "Generating answer..."), refresh_per_second=10):
            response = exa.client.answer(
                query,
                text=not raw_response,
            )

        # Extract and print answer text
        answer_text = exa._extract_answer_text(response)
        console.print(answer_text)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
