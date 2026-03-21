"""SESSION_END hook that extracts user memories after each personal conversation."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.message import Message

from ..extractor import (
    DEFAULT_MODEL,
    SENTINEL_FILENAME,
    USER_MEMORIES_FILE,
    _get_anthropic_api_key,
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
    merges them into ~/.config/gptme/user-memories/facts.md.

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

    # We read conversation.jsonl directly rather than using `manager.log` because
    # run_batch (the backfill path) also reads from disk, keeping both paths consistent.
    # gptme writes messages to disk eagerly, so the on-disk file is complete by the
    # time SESSION_END fires.  If that assumption ever changes, switch to manager.log.
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

    if not _get_anthropic_api_key():
        logger.warning(
            "user_memories: ANTHROPIC_API_KEY not configured — skipping extraction "
            "(set env var or add to config.toml). This session will be retried once a key is available."
        )
        # Do not touch sentinel — retry when a key is configured
        return

    model = os.environ.get("GPTME_MEMORIES_MODEL", DEFAULT_MODEL)
    facts = extract_facts(text, model=model)
    if facts is None:
        # Transient API failure — do NOT touch sentinel so the session is retried next run
        # (extract_facts already logs the warning with exception details)
        return
    if not facts:
        logger.debug("user_memories: no new facts extracted")
        sentinel.touch()
        return

    try:
        existing = load_existing_memories(USER_MEMORIES_FILE)
        merged = merge_facts(existing, facts)

        if merged != existing:
            new_count = len(merged) - len(existing)
            save_memories(USER_MEMORIES_FILE, merged)
            if new_count > 0:
                logger.info(
                    "user_memories: added %d new facts (total: %d)",
                    new_count,
                    len(merged),
                )
            elif new_count < 0:
                logger.debug(
                    "user_memories: cleaned up %d duplicate facts (total: %d)",
                    len(existing) - len(merged),
                    len(merged),
                )
            else:
                logger.debug(
                    "user_memories: updated memories — deduplicated existing + added new (total: %d)",
                    len(merged),
                )
        else:
            logger.debug("user_memories: all extracted facts were duplicates")

        # Only mark processed after a successful save (or no-op dedup)
        try:
            sentinel.touch()
        except OSError as e:
            logger.warning("user_memories: failed to touch sentinel: %s", e)
    except Exception as e:
        logger.warning("user_memories: failed to save memories: %s", e)
        # Do NOT touch sentinel — transient failure, retry on next session

    # gptme's hook framework requires Generator[Message, None, None] return type.
    # This stub makes CPython compile the function as a generator without any yields.
    if False:  # pragma: no cover
        yield Message("system", "")


def register() -> None:
    """Register user memories hook with gptme."""
    register_hook(
        name="user_memories.session_end_extract",
        hook_type=HookType.SESSION_END,
        func=session_end_user_memories_hook,
        priority=-20,
    )
