"""Token storage utilities for managing authentication tokens in .env files."""

import shutil
import tempfile
from pathlib import Path
from typing import List


def _read_env_lines(env_path: Path) -> List[str]:
    """Read lines from .env file."""
    try:
        with open(env_path) as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def _find_token_line_index(env_lines: List[str], token_name: str) -> int | None:
    """Find index of existing token line."""
    search_prefix = f"{token_name}="
    for i, line in enumerate(env_lines):
        if line.startswith(search_prefix):
            return i
    return None


def _write_env_atomically(env_path: Path, env_lines: List[str]) -> bool:
    """Write .env file atomically using temporary file."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=env_path.parent, prefix=".env.tmp"
        ) as tmp_file:
            tmp_file.writelines(env_lines)
            tmp_path = Path(tmp_file.name)

        shutil.move(str(tmp_path), str(env_path))
        return True
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
        return False


def save_token_to_env(
    token_name: str,
    token_value: str,
    env_path: Path | None = None,
    comment: str | None = None,
) -> bool:
    """Save or update a token in .env file.

    Args:
        token_name: Environment variable name (e.g., "TWITTER_OAUTH2_ACCESS_TOKEN")
        token_value: Token value to save
        env_path: Path to .env file (defaults to find_dotenv())
        comment: Optional comment to add before new tokens

    Returns:
        True if successful, False otherwise

    Example:
        >>> save_token_to_env("TWITTER_TOKEN", "abc123")
        True
    """
    from dotenv import find_dotenv

    # Find .env file if not provided
    if env_path is None:
        env_path_str = find_dotenv()
        if not env_path_str:
            return False
        env_path = Path(env_path_str)

    # Read existing content
    env_lines = _read_env_lines(env_path)

    # Find existing token line
    token_line_idx = _find_token_line_index(env_lines, token_name)

    # Prepare new token line
    new_token_line = f"{token_name}={token_value}\n"

    # Update or append
    if token_line_idx is not None:
        env_lines[token_line_idx] = new_token_line
    else:
        # Append new token with optional comment
        if comment:
            env_lines.extend(["\n", f"# {comment}\n", new_token_line])
        else:
            env_lines.append(new_token_line)

    # Write atomically
    return _write_env_atomically(env_path, env_lines)
