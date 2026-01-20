"""Main entry point for gptodo CLI.

Provides a unified command-line interface for task management operations.
Supports both direct invocation (`python -m gptodo`) and package entry point.
"""

from gptodo.cli import cli

if __name__ == "__main__":
    cli()
