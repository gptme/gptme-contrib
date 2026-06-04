"""Headroom SmartCrusher generation_pre hook for gptme.

Applies lossless SmartCrusher compression to structured/tabular tool outputs
before they reach the tooloutput-trimmer. Unstructured outputs pass through
unchanged and are handled by the trimmer.

SmartCrusher is imported lazily — if headroom-ai is not installed, the hook
gracefully disables itself with a warning.

Configuration via gptme.toml:

    [plugin.headroom_compressor]
    enabled = true
    raw_tool_prefixes = ["cat ", "echo "]
    min_compress_chars = 2000

Or via env: GPTME_HEADROOM_ENABLED=1
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from gptme.config import get_config
from gptme.hooks import HookType, StopPropagation
from gptme.message import Message

try:
    from gptme.util.cost_tracker import CostTracker
except ModuleNotFoundError:

    class CostTracker:  # type: ignore
        """Compatibility shim for older gptme releases."""

        @staticmethod
        def get_session_costs() -> None:
            return None


logger = logging.getLogger(__name__)

# Feature-flag env var
ENABLED_ENV_VAR = "GPTME_HEADROOM_ENABLED"

# Minimum content length to attempt compression
DEFAULT_MIN_COMPRESS_CHARS = 2000

# Tool output prefixes — same as the trimmer's TOOL_OUTPUT_PREFIXES
TOOL_OUTPUT_PREFIXES = ("Ran command: `", "Executed code block.")

# Sentinel string prepended to compressed content
COMPRESSED_MARKER = "[Headroom compressed"

# Session-level stats accumulator
_compressed_count: int = 0
_total_savings: int = 0


@dataclass(frozen=True)
class HeadroomCompressorConfig:
    """Configuration for the headroom compressor plugin.

    Read from [plugin.headroom_compressor] in gptme config + env var override.
    """

    enabled: bool = False
    min_compress_chars: int = DEFAULT_MIN_COMPRESS_CHARS
    # Commands/prefixes whose shell output should skip SmartCrusher compression.
    # Only applies to "Ran command: `...`" messages.
    raw_tool_prefixes: tuple[str, ...] = field(default_factory=tuple)


_SmartCrusher = None  # type: ignore


def _coerce_int(value: Any, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _coerce_raw_tool_prefixes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list | tuple):
        logger.warning(
            "headroom_compressor: ignoring invalid raw_tool_prefixes=%r; "
            "expected string or list[str]",
            value,
        )
        return ()
    prefixes = tuple(p for p in value if isinstance(p, str))
    if len(prefixes) != len(value):
        logger.warning(
            "headroom_compressor: ignoring non-string raw_tool_prefixes entries: %r",
            value,
        )
    return prefixes


def get_compressor_config() -> HeadroomCompressorConfig:
    """Read compressor config from env + [plugin.headroom_compressor]."""
    config = get_config()

    # Read plugin settings from gptme config
    settings: dict[str, Any] = {}
    if config.user and hasattr(config.user, "plugin"):
        settings = getattr(config.user, "plugin", {}).get("headroom_compressor", {})
    if config.project and hasattr(config.project, "plugin"):
        project_settings = getattr(config.project, "plugin", {}).get(
            "headroom_compressor", {}
        )
        settings = {**settings, **project_settings}

    # Env var overrides config file
    if hasattr(config, "get_env_bool"):
        env_enabled = config.get_env_bool(ENABLED_ENV_VAR)
    else:
        raw = os.environ.get(ENABLED_ENV_VAR, "").strip().lower()
        env_enabled = True if raw in ("1", "true", "yes") else None
    file_enabled = bool(settings.get("enabled"))
    enabled = env_enabled if env_enabled is not None else file_enabled

    return HeadroomCompressorConfig(
        enabled=enabled,
        min_compress_chars=_coerce_int(
            settings.get("min_compress_chars"),
            DEFAULT_MIN_COMPRESS_CHARS,
            minimum=1,
        ),
        raw_tool_prefixes=_coerce_raw_tool_prefixes(
            settings.get("raw_tool_prefixes", [])
        ),
    )


def _get_crusher():
    """Lazy-init SmartCrusher. Returns None if headroom not available."""
    global _SmartCrusher
    if _SmartCrusher is not None:
        if _SmartCrusher is False:  # sentinel — import previously failed
            return None
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


def _content_matches_raw_prefix(content: str, raw_prefixes: tuple[str, ...]) -> bool:
    """Check if a shell tool-output message's command matches a raw prefix."""
    if not raw_prefixes:
        return False
    first_line = content.splitlines()[0] if content else ""
    if m := re.search(r"`([^`]+)`", first_line):
        cmd = m.group(1)
        for prefix in raw_prefixes:
            if cmd.startswith(prefix):
                return True
    return False


def _is_compressible_message(
    message: Message,
    min_chars: int = DEFAULT_MIN_COMPRESS_CHARS,
    raw_prefixes: tuple[str, ...] = (),
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
    if _content_matches_raw_prefix(message.content, raw_prefixes):
        return False
    return True


def _split_tool_output(content: str) -> tuple[str, str]:
    """Split tool output into (prefix_line, data).

    The first line (command prefix like "Ran command: `...`") is separated
    from the remaining data. SmartCrusher needs pure data to recognize
    structured content types (JSON, tables, etc.).
    """
    idx = content.find("\n")
    if idx == -1:
        return content, ""
    return content[: idx + 1], content[idx + 1 :]


def generation_pre_hook(
    messages: list[Message],
    **kwargs: Any,
) -> Generator[Message | StopPropagation, None, None]:
    """Iterate tool output messages and apply SmartCrusher compression.

    Runs at priority 201 (before the trimmer at 200). Transforms large
    structured tool outputs losslessly. Unstructured/passthrough outputs
    are left for the trimmer.
    """
    global _compressed_count, _total_savings

    config = get_compressor_config()
    if not config.enabled:
        return

    crusher = _get_crusher()
    if crusher is None:
        return

    rewritten: list[Message] = []
    session_compressed = 0
    session_savings = 0

    for message in messages:
        if _is_compressible_message(
            message,
            min_chars=config.min_compress_chars,
            raw_prefixes=config.raw_tool_prefixes,
        ):
            try:
                prefix, data = _split_tool_output(message.content)
                result = crusher.crush(data)
                if result.was_modified:
                    original_len = len(message.content)
                    compressed_len = len(prefix) + len(result.compressed)
                    savings_pct = round((1 - compressed_len / original_len) * 100)
                    session_savings += original_len - compressed_len
                    session_compressed += 1
                    rewritten.append(
                        message.replace(
                            content=(
                                f"{COMPRESSED_MARKER} "
                                f"(orig={original_len}, "
                                f"strategy={result.strategy}, "
                                f"savings={savings_pct}%)]\n"
                                f"{prefix}{result.compressed}"
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

    if session_compressed > 0:
        messages[:] = rewritten
        _compressed_count += session_compressed
        _total_savings += session_savings

        # Record to cost tracker
        try:
            tracker = CostTracker.get_session_costs()
            if tracker is not None:
                tracker.record_extra(
                    key="headroom_compressor",
                    compressed_count=session_compressed,
                    chars_saved=session_savings,
                )
        except Exception:
            pass

        logger.info(
            "headroom_compressor: compressed %d message(s), saved %d chars "
            "(total this process: %d messages, %d chars)",
            session_compressed,
            session_savings,
            _compressed_count,
            _total_savings,
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
