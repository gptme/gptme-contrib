"""Headroom SmartCrusher generation_pre hook for gptme.

Applies lossless SmartCrusher compression to structured/tabular tool outputs
before they reach the tooloutput-trimmer. Unstructured outputs pass through
unchanged and are handled by the trimmer.

SmartCrusher is imported lazily — if headroom-ai is not installed, the hook
gracefully disables itself with a warning.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from typing import Any

from gptme.hooks import HookType, StopPropagation
from gptme.message import Message

logger = logging.getLogger(__name__)

# Feature-flag env var
ENABLED_ENV_VAR = "GPTME_HEADROOM_ENABLED"

# Minimum content length to attempt compression
DEFAULT_MIN_COMPRESS_CHARS = 2000

# Tool output prefixes — same as the trimmer's TOOL_OUTPUT_PREFIXES
TOOL_OUTPUT_PREFIXES = ("Ran command: `", "Executed code block.")

# Sentinel string prepended to compressed content
COMPRESSED_MARKER = "[Headroom compressed"

# Lazy-loaded SmartCrusher
_SmartCrusher = None  # type: ignore


def _get_crusher():
    """Lazy-init SmartCrusher. Returns None if headroom not available."""
    global _SmartCrusher
    if _SmartCrusher is not None:
        return _SmartCrusher()

    try:
        from headroom.transforms.smart_crusher import SmartCrusher as _Cls

        _SmartCrusher = _Cls
        logger.info("headroom_compressor: SmartCrusher loaded")
        return _SmartCrusher()
    except ImportError:
        logger.warning(
            "headroom_compressor: headroom-ai not installed; "
            "install with: pip install headroom-ai"
        )
        _SmartCrusher = False  # sentinel — don't retry
        return None
    except Exception:
        logger.warning("headroom_compressor: SmartCrusher init failed", exc_info=True)
        _SmartCrusher = False
        return None


def _check_enabled() -> bool:
    val = os.environ.get(ENABLED_ENV_VAR, "").strip().lower()
    return val in ("1", "true", "yes")


def _is_compressible_message(
    message: Message,
    min_chars: int = DEFAULT_MIN_COMPRESS_CHARS,
) -> bool:
    """Check if a message is a tool output worth attempting compression on."""
    if message.role != "system" or message.pinned:
        return False
    if not message.content:
        return False
    if message.content.startswith(COMPRESSED_MARKER):
        return False
    if not message.content.startswith(TOOL_OUTPUT_PREFIXES):
        return False
    if len(message.content) < min_chars:
        return False
    return True


def generation_pre_hook(
    messages: list[Message],
    **kwargs: Any,
) -> Generator[Message | StopPropagation, None, None]:
    """Iterate tool output messages and apply SmartCrusher compression.

    Runs at priority 201 (before the trimmer at 200). Transforms large
    structured tool outputs losslessly. Unstructured/passthrough outputs
    are left for the trimmer.
    """
    if not _check_enabled():
        return

    crusher = _get_crusher()
    if crusher is None:
        return

    rewritten: list[Message] = []
    compressed_count = 0
    total_savings = 0

    for message in messages:
        if _is_compressible_message(message):
            try:
                result = crusher.crush(message.content)
                if result.was_modified:
                    original_len = len(result.original)
                    compressed_len = len(result.compressed)
                    savings_pct = round((1 - compressed_len / original_len) * 100)
                    total_savings += original_len - compressed_len
                    compressed_count += 1
                    rewritten.append(
                        message.replace(
                            content=(
                                f"{COMPRESSED_MARKER} "
                                f"(orig={original_len}, "
                                f"strategy={result.strategy}, "
                                f"savings={savings_pct}%)]\n"
                                f"{result.compressed}"
                            )
                        )
                    )
                    continue
            except Exception:
                logger.debug(
                    "headroom_compressor: crush() failed on message",
                    exc_info=True,
                )
        rewritten.append(message)

    if compressed_count > 0:
        messages[:] = rewritten
        logger.info(
            "headroom_compressor: compressed %d message(s), saved %d chars",
            compressed_count,
            total_savings,
        )

    yield from ()


def register() -> None:
    """Register the SmartCrusher generation_pre hook."""
    from gptme.hooks import register_hook

    register_hook(
        name="headroom_compressor.generation_pre",
        hook_type=HookType.GENERATION_PRE,
        func=generation_pre_hook,
        priority=201,
    )
