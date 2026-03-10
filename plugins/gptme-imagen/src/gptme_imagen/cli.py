"""
Standalone CLI for gptme-imagen.

Usage:
    gptme-imagen generate "a sunset over mountains" --provider gemini
    gptme-imagen generate "tech logo" --style flat-design --count 3
    gptme-imagen generate "modify background" --images photo.png
    gptme-imagen cost
    gptme-imagen history --limit 10
"""

from __future__ import annotations

import sys

import click

from .tools.image_gen import (
    STYLE_PRESETS,
    ImageResult,
    generate_image,
)


@click.group()
@click.version_option(package_name="gptme-imagen")
def cli() -> None:
    """Multi-provider image generation CLI.

    Generate images from text prompts using Gemini, DALL-E, and more.
    Works standalone without the full gptme runtime.
    """


@cli.command()
@click.argument("prompt")
@click.option(
    "--provider",
    "-p",
    type=click.Choice(["gemini", "dalle", "dalle2"]),
    default="gemini",
    help="Image generation provider.",
)
@click.option("--output", "-o", type=str, default=None, help="Output file path.")
@click.option("--size", "-s", type=str, default="1024x1024", help="Image size.")
@click.option(
    "--quality",
    "-q",
    type=click.Choice(["standard", "hd"]),
    default="standard",
    help="Image quality.",
)
@click.option("--count", "-n", type=int, default=1, help="Number of images.")
@click.option(
    "--style",
    type=click.Choice(list(STYLE_PRESETS.keys())),
    default=None,
    help="Style preset.",
)
@click.option("--enhance", is_flag=True, help="Auto-enhance prompt.")
@click.option(
    "--images",
    "-i",
    multiple=True,
    help="Reference image(s) for multimodal generation (Gemini only).",
)
@click.option("--open", "open_file", is_flag=True, help="Open image after generation.")
def generate(
    prompt: str,
    provider: str,
    output: str | None,
    size: str,
    quality: str,
    count: int,
    style: str | None,
    enhance: bool,
    images: tuple[str, ...],
    open_file: bool,
) -> None:
    """Generate image(s) from a text prompt."""
    image_list: list[str] | None = list(images) if images else None

    try:
        result = generate_image(
            prompt=prompt,
            provider=provider,  # type: ignore[arg-type]
            size=size,
            quality=quality,
            output_path=output,
            count=count,
            view=False,
            style=style,  # type: ignore[arg-type]
            enhance=enhance,
            show_progress=True,
            images=image_list,
        )
    except (ValueError, FileNotFoundError, ImportError, RuntimeError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Print results
    results_list = [result] if isinstance(result, ImageResult) else result
    for img_result in results_list:
        click.echo(f"Saved: {img_result.image_path}")

    # Open files if requested
    if open_file:
        for img_result in results_list:
            click.launch(str(img_result.image_path))


@cli.command()
@click.option("--provider", "-p", type=str, default=None, help="Filter by provider.")
@click.option("--since", type=str, default=None, help="Start date (ISO format).")
@click.option("--until", type=str, default=None, help="End date (ISO format).")
def cost(provider: str | None, since: str | None, until: str | None) -> None:
    """Show generation cost breakdown."""
    from .tools.image_gen import get_cost_breakdown, get_total_cost

    total = get_total_cost(provider=provider, start_date=since, end_date=until)
    breakdown = get_cost_breakdown(start_date=since, end_date=until)

    if not breakdown:
        click.echo("No generation records found.")
        return

    click.echo("Cost Breakdown:")
    for prov, prov_cost in sorted(breakdown.items()):
        click.echo(f"  {prov}: ${prov_cost:.4f}")
    click.echo(f"  Total: ${total:.4f}")


@cli.command()
@click.option("--limit", "-n", type=int, default=10, help="Number of records.")
@click.option("--provider", "-p", type=str, default=None, help="Filter by provider.")
def history(limit: int, provider: str | None) -> None:
    """Show recent generation history."""
    from .tools.image_gen import get_generation_history

    records = get_generation_history(limit=limit, provider=provider)
    if not records:
        click.echo("No generation records found.")
        return

    for rec in records:
        ts = rec.get("timestamp", "?")
        prov = rec.get("provider", "?")
        prompt_text = rec.get("prompt", "")
        cost_val = rec.get("cost_usd", 0)
        # Truncate long prompts
        if len(prompt_text) > 60:
            prompt_text = prompt_text[:57] + "..."
        click.echo(f"  {ts}  {prov:8s}  ${cost_val:.4f}  {prompt_text}")


@cli.command()
def styles() -> None:
    """List available style presets."""
    click.echo("Available style presets:")
    for name, desc in STYLE_PRESETS.items():
        click.echo(f"  {name:20s} {desc}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
