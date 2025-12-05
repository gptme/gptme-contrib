"""Main entry point for tasks CLI.

Provides a unified command-line interface for task management operations.
Supports both direct invocation (`python -m tasks`) and package entry point.
"""

from tasks.cli import cli

if __name__ == "__main__":
    cli()
