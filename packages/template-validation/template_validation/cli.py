"""Command-line interface for template validation."""
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from .check_names import validate_names, validate_agent_identity


@click.group()
def main():
    """Template validation tools for gptme-agent-template."""
    pass


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["template", "fork"]),
    default="fork",
    help="Validation mode: template (strict) or fork (documentation-aware)",
)
@click.option(
    "--template-mode",
    "mode_flag",
    flag_value="template",
    help="Shorthand for --mode=template",
)
@click.option(
    "--fork-mode",
    "mode_flag",
    flag_value="fork",
    help="Shorthand for --mode=fork",
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Root directory to check",
)
@click.option(
    "--exclude",
    multiple=True,
    help="Additional patterns to exclude (can be specified multiple times)",
)
@click.option(
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Config file (.template-validation.yaml)",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Strict mode: also validate agent identity",
)
@click.option(
    "--suggest",
    is_flag=True,
    help="Show suggestions for fixing violations",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Verbose output",
)
def check_names(
    mode: Optional[str],
    mode_flag: Optional[str],
    root: Path,
    exclude: tuple,
    config: Optional[Path],
    strict: bool,
    suggest: bool,
    verbose: bool,
):
    """Check naming patterns in repository."""
    
    # Handle mode flags
    if mode_flag:
        mode = mode_flag
    
    # Load config if provided
    config_data = {}
    if config:
        try:
            config_data = yaml.safe_load(config.read_text())
        except Exception as e:
            click.echo(f"Error loading config: {e}", err=True)
            sys.exit(1)
            
    # Merge config with command-line options
    mode = config_data.get("mode", mode)
    excludes = list(exclude) + config_data.get("excludes", [])
    custom_patterns = config_data.get("patterns", {})
    
    if verbose:
        click.echo(f"Mode: {mode}")
        click.echo(f"Root: {root}")
        if excludes:
            click.echo(f"Excludes: {', '.join(excludes)}")
            
    # Run validation
    result = validate_names(
        root=root,
        mode=mode,
        excludes=excludes if excludes else None,
        custom_patterns=custom_patterns if custom_patterns else None,
    )
    
    # Print report
    click.echo(result.format_report())
    
    # Check agent identity in strict mode
    if strict:
        identity_errors = validate_agent_identity(root)
        if identity_errors:
            click.echo("\n✗ Agent identity validation failed:")
            for error in identity_errors:
                click.echo(f"  - {error}")
            sys.exit(1)
            
    # Show suggestions if requested and there are violations
    if suggest and not result.is_valid():
        click.echo("\nSuggestions:")
        if mode == "fork":
            click.echo("  - Remove or update references to 'gptme-agent-template'")
            click.echo("  - Replace template suffix in names")
        else:
            click.echo("  - Replace placeholder names with actual agent name")
            click.echo("  - Remove references to 'gptme-agent' (use 'gptme-agent-template')")
            
    # Exit with appropriate code
    sys.exit(0 if result.is_valid() else 1)


if __name__ == "__main__":
    main()
