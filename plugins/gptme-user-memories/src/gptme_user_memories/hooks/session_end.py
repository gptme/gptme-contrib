"""SESSION_END hook that extracts user memories after each personal conversation."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.message import Message

from ..extractor import (
    SENTINEL_FILENAME,
    USER_MEMORIES_FILE,
    extract_facts,
    get_user_messages,
    is_autonomous_session,
    load_existing_memories,
    merge_facts,
    save_memories,
)

if TYPE_CHECKING:
    from gptme.logmanager import LogManager

logger = logging.getLogger(__name__)


def session_end_user_memories_hook(
    manager: LogManager, logdir: Path | None = None, **kwargs
) -> Generator[Message, None, None]:
    """Run user memory extraction at session end.

    Extracts facts about the user from the just-completed conversation and
    merges them into ~/.local/share/gptme/user-memories.md.

    Args:
        manager: Active LogManager for this conversation
        logdir: Log directory for the conversation
        **kwargs: Additional hook args (ignored)

    Yields:
        Nothing. This hook logs status only.
    """
    if logdir is None:
        logger.debug("user_memories: missing logdir, skipping")
        return

    conv_file = logdir / "conversation.jsonl"
    if not conv_file.exists():
        logger.debug("user_memories: no conversation.jsonl in %s", logdir)
        return

    sentinel = logdir / SENTINEL_FILENAME
    if sentinel.exists():
        logger.debug("user_memories: sentinel exists, already processed")
        return

    if is_autonomous_session(conv_file):
        logger.debug("user_memories: skipping autonomous session")
        sentinel.touch()
        return

    text = get_user_messages(conv_file)
    if len(text) < 50:
        logger.debug("user_memories: conversation too short, skipping")
        sentinel.touch()
        return

    facts = extract_facts(text)
    if not facts:
        logger.debug("user_memories: no new facts extracted")
        sentinel.touch()
        return

    try:
        existing = load_existing_memories(USER_MEMORIES_FILE)
        merged = merge_facts(existing, facts)
        new_count = len(merged) - len(existing)

        if new_count > 0:
            save_memories(USER_MEMORIES_FILE, merged)
            logger.info(
                "user_memories: added %d new facts (total: %d)", new_count, len(merged)
            )
        else:
            logger.debug("user_memories: all extracted facts were duplicates")
    except Exception as e:
        logger.warning("user_memories: failed to save memories: %s", e)

    sentinel.touch()

    if False:  # makes this function a generator to satisfy Generator return type
        yield Message("system", "")


def register() -> None:
    """Register user memories hook with gptme."""
    register_hook(
        name="user_memories.session_end_extract",
        hook_type=HookType.SESSION_END,
        func=session_end_user_memories_hook,
        priority=-20,
    )
