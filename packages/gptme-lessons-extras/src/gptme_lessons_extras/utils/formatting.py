"""
Shared utilities for the learning system.

Contains helper functions used across analysis and lesson generation.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import click


def load_conversation(log_path: Path) -> List[Dict]:
    """Load conversation from JSONL log file."""
    messages: List[Dict] = []
    conversation_file = log_path / "conversation.jsonl"

    if not conversation_file.exists():
        click.echo(f"Error: Conversation file not found: {conversation_file}", err=True)
        return messages

    with open(conversation_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError as e:
                    click.echo(f"Warning: Failed to parse line: {e}", err=True)
                    continue

    return messages


def snippet(text: str, max_chars: int = 200) -> str:
    """Extract a snippet from text, truncating if needed."""
    if not text:
        return ""
    text = text.strip().replace("\r", "")
    return text[:max_chars]


def generate_slug(title: str, max_length: int = 50) -> str:
    """Generate a URL-safe slug from title."""
    slug = title.lower()
    slug = slug.replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return slug[:max_length]


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, creating if needed."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(data: Any, output_path: Path, indent: int = 2) -> Path:
    """Save data to JSON file."""
    ensure_dir(output_path.parent)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    return output_path


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO format timestamp string."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def is_failure_message(content: str) -> bool:
    """Check if message content indicates a failure."""
    if not content:
        return False
    keywords = ("Error", "Failed", "Traceback", "Exception", "Command not found")
    return any(k in content for k in keywords)


def is_success_message(content: str) -> bool:
    """Check if message content indicates a success."""
    if not content:
        return False
    keywords = (
        "Saved to",
        "Patch applied",
        "Appended to",
        "Created a pull request",
        "pull request",
        "PR ",
        "https://github.com",
        "✓ ",
    )
    return any(k in content for k in keywords)


def extract_tool_name_from_content(content: str) -> str | None:
    """Extract tool name from message content (simple heuristic)."""
    if not isinstance(content, str):
        return None

    tools = [
        "shell",
        "ipython",
        "patch",
        "save",
        "append",
        "read",
        "browser",
        "tmux",
        "gh",
        "screenshot",
        "vision",
    ]

    for tool in tools:
        if f"```{tool}" in content or f"`{tool} " in content:
            return tool

    return None


def count_tool_invocations(messages: List[Dict], start_idx: int, end_idx: int) -> int:
    """Count tool invocations in message range."""
    count = 0
    for i in range(start_idx, min(end_idx + 1, len(messages))):
        msg = messages[i]
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str) and "```" in content:
                count += 1
    return count


def format_duration(minutes: float | None) -> str:
    """Format duration in minutes to human-readable string."""
    if minutes is None:
        return "unknown"
    if minutes < 1:
        return f"{minutes * 60:.0f}s"
    elif minutes < 60:
        return f"{minutes:.1f}m"
    else:
        hours = minutes / 60
        return f"{hours:.1f}h"
