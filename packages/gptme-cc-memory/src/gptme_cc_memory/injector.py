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

import re
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
# Any level-1 or level-2 markdown header ends the current pending block.
_BLOCK_BOUNDARY_RE = re.compile(r"^#{1,2}\s")


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

    A block runs from its ``## Pending`` header up to the next level-1/2
    header (``# ``/``## ``), so its ``### Confirmations`` / ``### Guidance``
    subsections and any blank-line-separated paragraphs are pruned with it.
    Free-form content outside a dated block and blocks with unparseable dates
    are kept (fail-open). To append undated notes after a dated block, start
    a new level-1/2 header; blank lines alone do not end that block.
    """
    lines = content.splitlines(keepends=True)
    kept: list[str] = []
    skip_block = False

    for line in lines:
        match = _PENDING_BLOCK_HEADER_RE.match(line)
        if match:
            try:
                block_date = date.fromisoformat(match.group(1))
                skip_block = (today - block_date).days > PENDING_UPDATES_MAX_AGE_DAYS
            except (ValueError, TypeError):
                skip_block = False
        elif _BLOCK_BOUNDARY_RE.match(line):
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
    pending_session_context_file: Path | None = None,
    max_chars: int = MAX_INJECT_CHARS,
) -> str | None:
    """Build the memory injection block for a given prompt.

    Returns a string to inject into the session (or ``None`` if nothing is
    relevant / an error occurred).
    """
    try:
        # Sections are rendered in display order (guidance, pending updates,
        # session context, relevant memories) but admitted to the ``max_chars``
        # budget in priority order. Sections with side effects (one-shot files
        # cleared, memories recorded as injected) are included whole or not at
        # all, and their side effects only run when they are delivered.
        # Pending updates have no side effects, so they fill whatever budget
        # remains (truncated at a line boundary).
        guidance_block = ""
        if guidance_file:
            guidance = read_if_exists(guidance_file)
            if guidance:
                guidance_block = f"## Guidance\n\n{guidance}\n\n"

        pending_block = ""
        if pending_updates_file:
            updates = read_if_exists(pending_updates_file)
            if updates:
                pending_block = prune_stale_pending_updates(updates, date.today())

        context_block = ""
        if pending_session_context_file:
            ctx = read_if_exists(pending_session_context_file)
            if ctx:
                context_block = f"## Previous Session Context\n\n{ctx}\n\n"

        relevant = select_relevant_memories(
            prompt,
            memory_dir=memory_dir,
            state_file=metadata_file,
            limit=2,
        )
        memory_block = render_relevant_memory_block(relevant) if relevant else ""

        # Display slots: 0=guidance, 1=pending, 2=context, 3=memories
        included: dict[int, str] = {}

        def render(sections: dict[int, str]) -> str:
            return "\n".join(sections[i] for i in sorted(sections)).strip()

        def fits(slot: int, text: str) -> bool:
            return len(render({**included, slot: text})) <= max_chars

        for slot, text in ((0, guidance_block), (2, context_block), (3, memory_block)):
            if text and fits(slot, text):
                included[slot] = text

        if pending_block:
            if fits(1, f"{pending_block}\n\n"):
                included[1] = f"{pending_block}\n\n"
            else:
                # Room left after the join separator and trailing blank line,
                # then trim at a line boundary.
                room = max_chars - len(render(included)) - 3
                truncated = pending_block[: max(room, 0)]
                cut = truncated.rfind("\n")
                truncated = truncated[:cut].rstrip() if cut > 0 else ""
                if truncated and fits(1, f"{truncated}\n\n"):
                    included[1] = f"{truncated}\n\n"

        if not included:
            return None

        result = render(included)

        if 3 in included:
            record_memory_injections(
                [e["key"] for e in relevant],
                state_file=metadata_file,
            )
        if 0 in included and guidance_file:
            clear_file(guidance_file)
        if 2 in included and pending_session_context_file:
            clear_file(pending_session_context_file)

        return result

    except Exception:
        return None


if __name__ == "__main__":
    # The canonical CLI entry point is gptme-cc-memory-prompt-submit
    # (registered in pyproject.toml [project.scripts]), which calls
    # gptme_cc_memory.hooks.prompt_submit.main(). This __main__ guard
    # is kept for direct invocation via `python3 -m gptme_cc_memory.injector`.
    from gptme_cc_memory.hooks.prompt_submit import main as _main

    _main()
