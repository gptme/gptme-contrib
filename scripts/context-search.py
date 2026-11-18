#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["click>=8.0", "httpx>=0.27"]
# ///
"""Search public web sources with Context.dev from a gptme shell session."""

import json
import math
import os
from pathlib import Path
from typing import TypedDict

import click
import httpx
import tomllib

API_URL = "https://api.context.dev/v1/web/search"


class Highlights(TypedDict):
    code: str
    highlights: list[str] | None


class SearchResult(TypedDict):
    url: str
    title: str
    description: str
    highlights: Highlights


class SearchResponse(TypedDict):
    query: str
    results: list[SearchResult]
    partial: bool


def get_api_key() -> str:
    """Use the environment or gptme's existing [env] configuration."""
    if key := os.getenv("CONTEXT_DEV_API_KEY", "").strip():
        return key
    config_path = Path.home() / ".config" / "gptme" / "config.toml"
    if config_path.exists():
        try:
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise click.ClickException("Could not read gptme config.toml.") from None
        env = config.get("env", {})
        configured_key = (
            env.get("CONTEXT_DEV_API_KEY") if isinstance(env, dict) else None
        )
        if isinstance(configured_key, str) and configured_key.strip():
            return configured_key.strip()
    raise click.ClickException(
        "Set CONTEXT_DEV_API_KEY in the environment or [env] in ~/.config/gptme/config.toml."
    )


def parse_response(payload: object, query: str) -> SearchResponse:
    """Keep public evidence and content outcomes, excluding account metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise click.ClickException("Context.dev returned an unexpected response.")
    partial = payload.get("partial", False)
    if not isinstance(partial, bool):
        raise click.ClickException("Context.dev returned an unexpected response.")
    results: list[SearchResult] = []
    for result in payload["results"]:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("url"), str)
            or not result["url"]
        ):
            raise click.ClickException(
                "Context.dev returned an unexpected result response."
            )
        if not all(
            isinstance(result.get(field, ""), str) for field in ("title", "description")
        ):
            raise click.ClickException(
                "Context.dev returned an unexpected result response."
            )
        highlights = result.get("highlights")
        code = highlights.get("code") if isinstance(highlights, dict) else None
        code = code if isinstance(code, str) and code else "NOT_RETURNED"
        passages = (
            highlights.get("highlights")
            if isinstance(highlights, dict) and code == "SUCCESS"
            else None
        )
        if passages is not None and (
            not isinstance(passages, list)
            or not all(isinstance(passage, str) for passage in passages)
        ):
            raise click.ClickException(
                "Context.dev returned an unexpected Highlights response."
            )
        results.append(
            {
                "url": result["url"],
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "highlights": {"code": code, "highlights": passages},
            }
        )
    return {"query": query, "results": results, "partial": partial}


@click.command()
@click.argument("query")
@click.option("--results", type=click.IntRange(10, 100), default=10, show_default=True)
@click.option(
    "--highlights/--no-highlights",
    default=True,
    show_default=True,
    help="Request query-relevant passages; additional Context.dev credits apply.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print structured JSON for agents and scripts.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(0, 120),
    default=30,
    show_default=True,
)
def cli(
    query: str, results: int, highlights: bool, as_json: bool, timeout: float
) -> None:
    """Search the live web using Context.dev. Pass a public QUERY in quotes.

    Credentials come from CONTEXT_DEV_API_KEY or gptme's [env] configuration.
    Search snippets and Highlights are discovery evidence, not verified page reads.
    """
    query = query.strip()
    if not query:
        raise click.BadParameter("must not be blank", param_hint="QUERY")
    if len(query) > 500:
        raise click.BadParameter("must be at most 500 characters", param_hint="QUERY")
    if not math.isfinite(timeout) or timeout <= 0:
        raise click.BadParameter(
            "must be finite and greater than zero", param_hint="--timeout"
        )
    api_key = get_api_key()
    if not api_key.isascii() or any(
        ord(char) < 33 or ord(char) > 126 for char in api_key
    ):
        raise click.ClickException(
            "CONTEXT_DEV_API_KEY must contain printable ASCII without whitespace."
        )
    try:
        response = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "query": query,
                "numResults": results,
                "highlightsOptions": {"enabled": highlights},
            },
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        raise click.ClickException(
            "Context.dev request timed out; no retry was made."
        ) from None
    except httpx.HTTPError:
        raise click.ClickException(
            "Could not connect to Context.dev; no retry was made."
        ) from None
    if not response.is_success:
        raise click.ClickException(
            f"Context.dev request failed (HTTP {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError:
        raise click.ClickException(
            "Context.dev returned an invalid JSON response."
        ) from None
    data = parse_response(payload, query)
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if data["partial"]:
        click.echo(
            "Partial results: Context.dev did not finish collecting all results."
        )
    if not data["results"]:
        click.echo("No results returned.")
    for index, result in enumerate(data["results"], 1):
        click.echo(f"\n{index}. {result['title']}\n{result['url']}")
        outcome = result["highlights"]
        if outcome["code"] == "SUCCESS" and outcome["highlights"]:
            click.echo("Highlights (search evidence):")
            click.echo("\n".join(outcome["highlights"]))
        else:
            click.echo(f"Highlights: {outcome['code']}; search snippet:")
            click.echo(result["description"])


if __name__ == "__main__":
    cli()
