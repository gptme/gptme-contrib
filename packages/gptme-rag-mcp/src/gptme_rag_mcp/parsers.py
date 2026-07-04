"""Session log parsers for journal, gptme, and Claude Code formats.

Each parser produces SessionDocument objects — a common format for indexing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

# Book parsing utilities re-exported from gptme-wisdom to avoid duplication.
from gptme_wisdom.parsers import BookDocument, estimate_tokens, parse_book_text

__all__ = [
    "SessionDocument",
    "BookDocument",
    "estimate_tokens",
    "parse_book_text",
]


@dataclass
class SessionDocument:
    """A parsed session document ready for indexing.

    Represents a single searchable unit from any session source.
    """

    source: str  # "journal", "gptme", "claude_code"
    path: str  # absolute file path
    date: datetime  # session date
    title: str  # session title or slug
    content: str  # full text content for embedding
    summary: str  # first ~200 chars for display
    metadata: dict = field(default_factory=dict)
    # metadata examples: {session_type, trigger, outcome, model, duration, ...}


def parse_journal_entry(path: Path) -> SessionDocument | None:
    """Parse a journal markdown file into a SessionDocument.

    Handles both old-style (journal/YYYY-MM-DD.md) and new-style
    (journal/YYYY-MM-DD/HHMMSS-topic.md) formats.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None

    # Parse YAML frontmatter
    metadata: dict = {}
    content = text
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm_match:
        content = text[fm_match.end() :]
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                metadata[key.strip()] = val.strip()

    # Extract date from path
    date = _extract_date_from_journal_path(path)
    if date is None:
        return None

    # Extract title from first heading or filename
    title = _extract_title(content, path)

    # Build summary (first ~200 chars of content, stripped of markdown)
    summary = _make_summary(content, max_len=200)

    return SessionDocument(
        source="journal",
        path=str(path),
        date=date,
        title=title,
        content=content.strip(),
        summary=summary,
        metadata=metadata,
    )


def parse_gptme_log(log_dir: Path) -> SessionDocument | None:
    """Parse a gptme conversation log directory into a SessionDocument.

    Expects a directory containing conversation.jsonl with messages.
    """
    conv_file = log_dir / "conversation.jsonl"
    if not conv_file.exists():
        return None

    messages = []
    first_ts = None
    last_ts = None

    for line in conv_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = msg.get("role", "")
        content = _extract_message_text(msg.get("content", ""))
        ts_str = msg.get("timestamp", "")

        # Skip system messages (large boilerplate)
        if role == "system":
            continue

        # Track timestamps
        if ts_str:
            try:
                ts = _parse_ts(ts_str)
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            except ValueError:
                pass

        # Collect user and assistant messages
        if role in ("user", "assistant") and content:
            # Strip thinking blocks from assistant messages
            if role == "assistant":
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
            content = content.strip()
            if content:
                messages.append(f"[{role}] {content}")

    if not messages:
        return None

    # Extract date from directory name
    date = _extract_date_from_gptme_dir(log_dir, first_ts)
    if date is None:
        return None

    # Build full text
    full_text = "\n\n".join(messages)

    # Extract title from directory name
    title = _extract_gptme_title(log_dir)

    # Metadata
    metadata: dict = {}
    if first_ts and last_ts:
        duration_secs = (last_ts - first_ts).total_seconds()
        metadata["duration_minutes"] = round(duration_secs / 60, 1)
    metadata["message_count"] = len(messages)

    return SessionDocument(
        source="gptme",
        path=str(log_dir),
        date=date,
        title=title,
        content=full_text,
        summary=_make_gptme_summary(messages, max_len=200),
        metadata=metadata,
    )


def parse_claude_code_session(path: Path) -> SessionDocument | None:
    """Parse a Claude Code JSONL transcript into a SessionDocument."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None

    messages = []
    first_ts = None
    last_ts = None
    model = None
    slug = None
    total_input_tokens = 0
    total_output_tokens = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type", "")
        ts_str = entry.get("timestamp", "")

        # Track timestamps
        if ts_str:
            try:
                ts = _parse_ts(ts_str)
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            except ValueError:
                pass

        # Extract slug
        if not slug and entry.get("slug"):
            slug = entry["slug"]

        # Process user messages
        if entry_type == "user":
            msg = entry.get("message", {})
            content = _extract_message_text(msg.get("content", ""))
            if content:
                messages.append(f"[user] {content}")

        # Process assistant messages
        elif entry_type == "assistant":
            msg = entry.get("message", {})
            if not model and msg.get("model"):
                model = msg["model"]

            # Track tokens
            usage = msg.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            # Extract text content
            content_parts = msg.get("content", [])
            if isinstance(content_parts, list):
                for part in content_parts:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_content = part.get("text", "")
                            if text_content:
                                messages.append(f"[assistant] {text_content}")
                        elif part.get("type") == "tool_use":
                            tool_name = part.get("name", "unknown")
                            messages.append(f"[tool:{tool_name}]")

    if not messages:
        return None

    date = first_ts or datetime.now()

    full_text = "\n\n".join(messages)
    title = slug or path.stem

    metadata: dict = {}
    if model:
        metadata["model"] = model
    if first_ts and last_ts:
        metadata["duration_minutes"] = round((last_ts - first_ts).total_seconds() / 60, 1)
    metadata["message_count"] = len(messages)
    if total_input_tokens:
        metadata["input_tokens"] = total_input_tokens
    if total_output_tokens:
        metadata["output_tokens"] = total_output_tokens

    return SessionDocument(
        source="claude_code",
        path=str(path),
        date=date,
        title=title,
        content=full_text,
        summary=_make_summary(full_text, max_len=200),
        metadata=metadata,
    )


# --- Iterators for discovering session files ---


def iter_journal_entries(journal_dir: Path) -> Iterator[Path]:
    """Yield all journal entry markdown files.

    Skips entries under /tmp/worktrees/ to avoid indexing duplicate copies
    of the same journal entries from git worktrees.
    """
    if not journal_dir.exists():
        return

    # Skip worktree copies — they duplicate main repo entries
    resolved = str(journal_dir.resolve())
    if "/tmp/worktrees/" in resolved:
        return

    for item in sorted(journal_dir.iterdir()):
        if item.suffix == ".md" and item.is_file() and re.match(r"\d{4}-\d{2}-\d{2}", item.stem):
            yield item
        elif item.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", item.name):
            for md_file in sorted(item.glob("*.md")):
                yield md_file


def iter_gptme_logs(logs_dir: Path) -> Iterator[Path]:
    """Yield all gptme log directories containing conversation.jsonl."""
    if not logs_dir.exists():
        return

    for item in sorted(logs_dir.iterdir()):
        if item.is_dir() and (item / "conversation.jsonl").exists():
            yield item


def iter_claude_code_sessions(projects_dir: Path) -> Iterator[Path]:
    """Yield all Claude Code JSONL transcript files."""
    if not projects_dir.exists():
        return

    for subdir in sorted(projects_dir.iterdir()):
        if subdir.is_dir():
            for jsonl_file in sorted(subdir.glob("*.jsonl")):
                yield jsonl_file


def iter_cursor_sessions(home_dir: Path | None = None) -> Iterator[Path]:
    """Yield all Cursor conversation JSON files.

    Checks two Cursor storage layouts:
    - Standard: ~/.cursor/conversations/<uuid>/conversation.json
    - Alternate (0.40+): ~/.cursor/User/workspaceStorage/<hash>/cursor-chat-history.json
    """
    base = home_dir or Path.home()
    cursor_dir = base / ".cursor" / "conversations"
    if cursor_dir.is_dir():
        for item in sorted(cursor_dir.iterdir()):
            if item.is_dir():
                conv_file = item / "conversation.json"
                if conv_file.exists():
                    yield conv_file
            elif item.suffix == ".json":
                yield item
    ws_storage = base / ".cursor" / "User" / "workspaceStorage"
    if ws_storage.is_dir():
        for hash_dir in sorted(ws_storage.iterdir()):
            chat_file = hash_dir / "cursor-chat-history.json"
            if chat_file.exists():
                yield chat_file


def iter_codex_sessions(home_dir: Path | None = None) -> Iterator[Path]:
    """Yield all OpenAI Codex CLI JSONL session files from ~/.codex/sessions/."""
    base = home_dir or Path.home()
    codex_dir = base / ".codex" / "sessions"
    if codex_dir.is_dir():
        yield from sorted(codex_dir.glob("*.jsonl"))


def parse_cursor_session(path: Path) -> SessionDocument | None:
    """Parse a Cursor conversation JSON into a SessionDocument.

    Handles both the standard messages[] format and the alternate
    allConversations[].bubbles format used in Cursor 0.40+.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None

    messages_raw = data.get("messages", [])
    if not messages_raw:
        all_convs = data.get("allConversations", [])
        if not all_convs:
            return None
        for conv in all_convs:
            for bubble in conv.get("bubbles", []):
                role = "user" if bubble.get("type") == "user" else "assistant"
                text = bubble.get("text") or bubble.get("rawResponse", {}).get("text", "")
                messages_raw.append({"role": role, "content": text, "timestamp": None})

    first_ts = None
    last_ts = None
    messages = []

    for msg in messages_raw:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts_raw = msg.get("timestamp")

        if ts_raw is not None:
            try:
                ts = _parse_ts(ts_raw)
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            except (ValueError, OSError):
                pass

        if content and content.strip():
            messages.append(f"[{role}] {content.strip()}")

    if not messages:
        return None

    title = data.get("title") or _extract_first_user_intent(messages) or path.stem
    date = first_ts or _ts_from_path(path) or datetime.now()
    full_text = "\n\n".join(messages)
    metadata: dict = {
        "message_count": len(messages),
        "session_id": data.get("id", path.stem),
    }
    if first_ts and last_ts:
        metadata["duration_minutes"] = round((last_ts - first_ts).total_seconds() / 60, 1)

    return SessionDocument(
        source="cursor",
        path=str(path),
        date=date,
        title=title,
        content=full_text,
        summary=_make_summary(full_text, max_len=200),
        metadata=metadata,
    )


def parse_codex_session(path: Path) -> SessionDocument | None:
    """Parse an OpenAI Codex CLI JSONL session into a SessionDocument."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None

    messages = []
    first_ts = None
    last_ts = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type", "")
        ts_str = entry.get("timestamp", "")

        if ts_str:
            try:
                ts = _parse_ts(ts_str)
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            except ValueError:
                pass

        if entry_type == "message":
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if content and content.strip():
                messages.append(f"[{role}] {content.strip()}")
        elif entry_type == "tool_call":
            tool_name = entry.get("name", "unknown")
            inp = entry.get("input", {})
            cmd = inp.get("command", inp.get("code", ""))
            if cmd:
                messages.append(f"[tool:{tool_name}] {str(cmd)[:200]}")
            else:
                messages.append(f"[tool:{tool_name}]")
        elif entry_type == "tool_result":
            output = entry.get("output", "")
            if output:
                messages.append(f"[result] {str(output)[:300]}")

    if not messages:
        return None

    date = first_ts or _ts_from_path(path) or datetime.now()
    full_text = "\n\n".join(messages)
    metadata: dict = {"message_count": len(messages)}
    if first_ts and last_ts:
        metadata["duration_minutes"] = round((last_ts - first_ts).total_seconds() / 60, 1)

    return SessionDocument(
        source="codex",
        path=str(path),
        date=date,
        title=path.stem,
        content=full_text,
        summary=_make_summary(full_text, max_len=200),
        metadata=metadata,
    )


# --- Helper functions ---


def _parse_ts(ts_str: object) -> datetime:
    """Parse a timestamp string, normalizing to naive datetime (strips timezone)."""
    if isinstance(ts_str, (int, float)):
        if ts_str > 1e12:
            ts_str = ts_str / 1000
        return datetime.fromtimestamp(ts_str)
    if not isinstance(ts_str, str):
        raise ValueError(f"Unsupported timestamp type: {type(ts_str).__name__}")
    # Handle Z suffix
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    # Strip timezone to avoid naive/aware comparison issues
    return dt.replace(tzinfo=None)


def _extract_message_text(content: object) -> str:
    """Extract searchable text from heterogeneous message payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_extract_message_text(part) for part in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        parts = [_extract_message_text(content.get(key)) for key in ("text", "content")]
        return "\n".join(part for part in parts if part)
    return ""


def _extract_date_from_journal_path(path: Path) -> datetime | None:
    """Extract date from journal file path."""
    # New style: journal/YYYY-MM-DD/HHMMSS-topic.md
    parent_match = re.match(r"(\d{4}-\d{2}-\d{2})", path.parent.name)
    if parent_match:
        date_str = parent_match.group(1)
        # Try to extract time from filename
        time_match = re.match(r"(\d{6})-", path.stem)
        if time_match:
            time_str = time_match.group(1)
            try:
                return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M%S")
            except ValueError:
                pass
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None

    # Old style: journal/YYYY-MM-DD.md
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", path.stem)
    if date_match:
        try:
            return datetime.strptime(date_match.group(1), "%Y-%m-%d")
        except ValueError:
            return None

    return None


def _extract_date_from_gptme_dir(log_dir: Path, first_ts: datetime | None) -> datetime | None:
    """Extract date from gptme log directory name or first message timestamp."""
    # Try date-slug format: 2025-08-30-jumping-orange-walrus
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", log_dir.name)
    if date_match:
        try:
            return datetime.strptime(date_match.group(1), "%Y-%m-%d")
        except ValueError:
            pass

    # Try numeric timestamp (milliseconds)
    if log_dir.name.isdigit():
        try:
            ts = int(log_dir.name)
            if ts > 1e12:  # milliseconds
                ts = ts // 1000
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            pass

    # Fall back to first message timestamp
    return first_ts


def _extract_gptme_title(log_dir: Path) -> str:
    """Extract a readable title from gptme log directory name."""
    # Strip date prefix if present
    name = log_dir.name
    name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)
    # Strip numeric-only names
    if name.isdigit():
        return f"session-{name}"
    return name.replace("-", " ")


def _extract_title(content: str, path: Path) -> str:
    """Extract a descriptive title from markdown content or filename.

    For journal entries, the first heading is often generic ("Session", "Operator
    Session"). In that case, look for a more descriptive subtitle (H2) or include
    the session category/model from frontmatter.
    """
    # Try H1 first
    h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if h1_match:
        title = h1_match.group(1).strip()
        # If generic, try to enrich with subtitle
        generic_patterns = [
            r"^(Autonomous |Operator )?Session\b",
            r"^Session \w+$",
        ]
        is_generic = any(re.match(p, title) for p in generic_patterns)
        if is_generic:
            # Look for a descriptive H2 subtitle
            h2_match = re.search(r"^##\s+(.+)$", content, re.MULTILINE)
            if h2_match:
                subtitle = h2_match.group(1).strip()
                # Skip structural headings
                if subtitle.lower() not in (
                    "context",
                    "what i did",
                    "assessment",
                    "artifacts",
                    "commits",
                    "subtasks",
                    "references",
                    "step 1",
                ):
                    return f"{title}: {subtitle}"
        return title

    # Fall back to any heading level
    heading_match = re.search(r"^#+\s+(.+)$", content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()

    # Fall back to filename
    return path.stem.replace("-", " ").title()


def _make_gptme_summary(messages: list[str], max_len: int = 200) -> str:
    """Create a useful summary from gptme conversation messages.

    Skips boilerplate prompt file references and autonomous run preambles,
    looking for the actual user intent or first assistant response.
    """
    # Patterns to skip in first user message
    _skip_patterns = [
        r"^\[user\]\s*'?Here is the prompt to follow:",
        r"^\[user\]\s*/home/.*\.txt\b",
        r"^\[user\]\s*/tmp/autonomous-prompt",
        r"^\[user\]\s*You are Bob, running autonomously",
    ]

    for msg in messages:
        # Skip messages matching boilerplate patterns
        skip = False
        for pattern in _skip_patterns:
            if re.match(pattern, msg):
                skip = True
                break
        if skip:
            continue

        # Strip the [role] prefix for summary
        clean = re.sub(r"^\[(user|assistant)\]\s*", "", msg)
        clean = re.sub(r"[#*_`~\[\]()]", "", clean)
        clean = re.sub(r"\n+", " ", clean).strip()
        if clean and len(clean) > 20:
            if len(clean) > max_len:
                clean = clean[:max_len].rsplit(" ", 1)[0] + "..."
            return clean

    # Fallback: use first message raw
    return _make_summary("\n".join(messages), max_len)


def _make_summary(text: str, max_len: int = 200) -> str:
    """Create a short summary from text content."""
    # Strip markdown formatting
    clean = re.sub(r"[#*_`~\[\]()]", "", text)
    clean = re.sub(r"\n+", " ", clean)
    clean = clean.strip()
    if len(clean) > max_len:
        clean = clean[:max_len].rsplit(" ", 1)[0] + "..."
    return clean


def _ts_from_path(path: Path) -> datetime | None:
    """Extract a timestamp from file mtime as a fallback."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _extract_first_user_intent(messages: list[str]) -> str | None:
    """Extract the first user message as a title hint.

    Returns the first 80 chars of the first [user] message, or None.
    """
    for msg in messages:
        if msg.startswith("[user] "):
            text = msg[7:].strip()
            if text:
                return text[:80]
    return None
