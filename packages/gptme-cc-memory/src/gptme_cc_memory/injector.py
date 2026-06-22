"""UserPromptSubmit hook — inject cross-session memory context into CC.

This module implements the ``prompt-inject`` pattern: it reads the memory
directory, scores each file against the current prompt, and renders the
top-N relevant memories as a context block for injection.

Design principles:
    - Zero API cost — pure file reads, no LLM calls
    - <100ms typical execution
    - Graceful degradation — any error => silent exit (no injection)
    - Auto-clears one-shot guidance after successful delivery
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from gptme_cc_memory.memory_retrieval import (
    record_memory_injections,
    render_relevant_memory_block,
    select_relevant_memories,
)

# Maximum injection size (characters) — keeps token budget reasonable
MAX_INJECT_CHARS = 4000

# How many days a dated pending-updates block keeps re-injecting before it is
# pruned. Blocks are written as "## Pending — YYYY-MM-DD HH:MM (session: ...)"
# by the stop-hook extractor. After this many days the signal is stale.
PENDING_UPDATES_MAX_AGE_DAYS = 3

_PENDING_BLOCK_HEADER_RE = re.compile(r"^## Pending — (\d{4}-\d{2}-\d{2})", re.MULTILINE)


def read_if_exists(path: Path) -> str:
    """Read file content if it exists and is non-empty."""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError:
        pass
    return ""


def clear_file(path: Path) -> None:
    """Clear a file's content (auto-clear after delivery)."""
    try:
        path.write_text("")
    except OSError:
        pass


def prune_stale_pending_updates(content: str, today: date) -> str:
    """Drop dated ``## Pending — YYYY-MM-DD ...`` blocks older than the max age.

    Free-form (undated) content and blocks with unparseable dates are always
    kept (fail-open).
    """
    lines = content.splitlines(keepends=True)
    kept: list[str] = []
    skip_block = False

    for line in lines:
        match = _PENDING_BLOCK_HEADER_RE.match(line)
        if match:
            try:
                block_date = date.fromisoformat(match.group(1))
                age = (today - block_date).days
                skip_block = age > PENDING_UPDATES_MAX_AGE_DAYS
                if skip_block:
                    continue
            except (ValueError, TypeError):
                skip_block = False
        elif line.startswith("#"):
            pass  # section headers don't toggle skip
        elif skip_block and line.strip() == "":
            skip_block = False

        if not skip_block:
            kept.append(line)

    return "".join(kept).strip()


def inject_memories(
    prompt: str,
    memory_dir: Path,
    metadata_file: Path,
    guidance_file: Path | None = None,
    pending_updates_file: Path | None = None,
    pending_items_file: Path | None = None,
    pending_session_context_file: Path | None = None,
    max_chars: int = MAX_INJECT_CHARS,
) -> str | None:
    """Build the memory injection block for a given prompt.

    Returns a string to inject into the session (or ``None`` if nothing is
    relevant / an error occurred).
    """
    try:
        blocks: list[str] = []

        # 1. Guidance (one-shot, cleared after delivery)
        if guidance_file:
            guidance = read_if_exists(guidance_file)
            if guidance:
                blocks.append(f"## Guidance\n\n{guidance}\n\n")
                clear_file(guidance_file)

        # 2. Pending updates (content with stale-date pruning)
        if pending_updates_file:
            updates = read_if_exists(pending_updates_file)
            if updates:
                pruned = prune_stale_pending_updates(updates, date.today())
                if pruned:
                    blocks.append(f"{pruned}\n\n")

        # 3. Pending items
        if pending_items_file:
            items = read_if_exists(pending_items_file)
            if items:
                blocks.append(f"## Pending Items\n\n{items}\n\n")

        # 4. Pending session context (one-shot)
        if pending_session_context_file:
            ctx = read_if_exists(pending_session_context_file)
            if ctx:
                blocks.append(f"## Previous Session Context\n\n{ctx}\n\n")
                clear_file(pending_session_context_file)

        # 5. Relevant memory entries (scored against prompt)
        relevant = select_relevant_memories(
            prompt,
            memory_dir=memory_dir,
            state_file=metadata_file,
            limit=2,
        )
        if relevant:
            rendered = render_relevant_memory_block(relevant)
            blocks.append(rendered)
            record_memory_injections(
                [e["key"] for e in relevant],
                state_file=metadata_file,
            )

        if not blocks:
            return None

        result = "\n".join(blocks).strip()
        if len(result) > max_chars:
            result = result[:max_chars]

        return result

    except Exception:
        return None


def main() -> None:
    """CLI entry point for the injector (UserPromptSubmit hook)."""
    # Read stdin: JSON with {session_id, transcript_path, prompt}
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return

    prompt = input_data.get("prompt", "")
    if not prompt:
        return

    # Resolve paths from environment or defaults
    workspace = Path(os.environ.get("GPTME_CC_MEMORY_DIR", os.getcwd()))
    memory_dir = workspace / "memory"
    state_dir = workspace / "state" / "cc-memory"
    metadata_file = state_dir / "metadata.json"

    if not memory_dir.is_dir():
        return

    guidance_file = memory_dir / "guidance.md"
    pending_updates_file = memory_dir / "pending-updates.md"
    pending_items_file = memory_dir / "pending-items.md"
    pending_session_context_file = memory_dir / "pending-session-context.md"

    result = inject_memories(
        prompt,
        memory_dir=memory_dir,
        metadata_file=metadata_file,
        guidance_file=guidance_file,
        pending_updates_file=pending_updates_file,
        pending_items_file=pending_items_file,
        pending_session_context_file=pending_session_context_file,
    )

    if result:
        print(result)


if __name__ == "__main__":
    main()
