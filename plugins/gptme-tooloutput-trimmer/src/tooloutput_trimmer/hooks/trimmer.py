"""Hooks for trimming old tool output before LLM calls.

Triggers are all *predicted-cold* or *pressure-based* — we only trim before a
request that we expect to miss cache anyway, so trimming has no extra cost.

The previous `confirmed_cache_miss` trigger (post-hoc, based on
`cache_read_input_tokens == 0`) was dropped on 2026-05-11: by the time it
fires, the cache miss + cache write have already been billed, so trimming the
*next* turn doesn't recover the cost we already paid.
See ErikBjare/bob#770 for Erik's design feedback.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from gptme.config import get_config
from gptme.hooks import HookType, StopPropagation, register_hook
from gptme.message import Message

try:
    from gptme.util.cost_tracker import CostTracker, SessionCosts
except ModuleNotFoundError:
    SessionCosts = Any

    class CostTracker:
        """Compatibility shim for older gptme releases."""

        @staticmethod
        def get_session_costs() -> None:
            return None


logger = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_RECENT_TURNS = 5
DEFAULT_PREVIEW_CHARS = 500
DEFAULT_PRESSURE_CHARS = 100_000
ANTHROPIC_CACHE_TTL_SECS = 5 * 60
TOOL_OUTPUT_PREFIXES = ("Ran command: `", "Executed code block.")
TRIMMED_MARKER = "[Tool output trimmed"
HEADROOM_MARKER = "[Headroom compressed"  # skip messages already losslessly compressed
SUMMARIZATION_MARKER = (
    "[Summarized previous tool outputs]"  # marker for summarization inserts
)
BYPASS_ENV_VAR = "GPTME_TRIM_BYPASS"
SUMMARIZE_ENV_VAR = "GPTME_SUMMARIZE_TOOL_OUTPUTS"


@dataclass(frozen=True)
class TrimmerConfig:
    enabled: bool
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    recent_turns: int = DEFAULT_RECENT_TURNS
    preview_chars: int = DEFAULT_PREVIEW_CHARS
    pressure_chars: int = DEFAULT_PRESSURE_CHARS
    # Policy-layering field (Phase 1 bypass contract):
    # Commands/prefixes whose shell output should stay raw (never trim).
    # This only applies to "Ran command: `...`" messages; "Executed code block."
    # payloads do not retain the original code string, so there is no command
    # prefix to match against.
    raw_tool_prefixes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TrimSummary:
    trimmed_count: int = 0
    saved_chars: int = 0


@dataclass(frozen=True)
class TriggerDecision:
    expected_cache_cold: bool = False
    cache_invalidated: bool = False
    pressure_exceeded: bool = False

    @property
    def aggressive(self) -> bool:
        return self.expected_cache_cold or self.cache_invalidated

    @property
    def active(self) -> bool:
        return self.aggressive or self.pressure_exceeded

    def describe(self) -> str:
        reasons: list[str] = []
        if self.expected_cache_cold:
            reasons.append("expected-cache-cold")
        if self.cache_invalidated:
            reasons.append("cache-invalidated")
        if self.pressure_exceeded:
            reasons.append("context-pressure")
        return ",".join(reasons) if reasons else "none"


def reset_state() -> None:
    """Kept for test compatibility; trimmer no longer carries cross-turn state."""
    return None


def _coerce_int(value: Any, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _get_plugin_settings() -> dict[str, Any]:
    config = get_config()
    user_cfg = {}
    project_cfg = {}
    if config.user and hasattr(config.user, "plugin"):
        user_cfg = getattr(config.user, "plugin", {}).get("tooloutput_trimmer", {})
    if config.project and hasattr(config.project, "plugin"):
        project_cfg = getattr(config.project, "plugin", {}).get(
            "tooloutput_trimmer", {}
        )
    return {**user_cfg, **project_cfg}


def _coerce_raw_tool_prefixes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list | tuple):
        logger.warning(
            "tooloutput_trimmer: ignoring invalid raw_tool_prefixes=%r; expected string or list[str]",
            value,
        )
        return ()

    prefixes = tuple(prefix for prefix in value if isinstance(prefix, str))
    if len(prefixes) != len(value):
        logger.warning(
            "tooloutput_trimmer: ignoring non-string raw_tool_prefixes entries: %r",
            value,
        )
    return prefixes


def get_trimmer_config() -> TrimmerConfig:
    """Read trimmer config from env + [plugin.tooloutput_trimmer]."""
    settings = _get_plugin_settings()
    config = get_config()
    env_enabled = config.get_env_bool("GPTME_READ_TIME_TRIMMER")
    enabled = env_enabled if env_enabled is not None else bool(settings.get("enabled"))
    return TrimmerConfig(
        enabled=enabled,
        max_output_chars=_coerce_int(
            settings.get("max_output_chars"),
            DEFAULT_MAX_OUTPUT_CHARS,
            minimum=1,
        ),
        recent_turns=_coerce_int(
            settings.get("recent_turns"),
            DEFAULT_RECENT_TURNS,
            minimum=1,
        ),
        preview_chars=_coerce_int(
            settings.get("preview_chars"),
            DEFAULT_PREVIEW_CHARS,
            minimum=0,
        ),
        pressure_chars=_coerce_int(
            settings.get("pressure_chars"),
            DEFAULT_PRESSURE_CHARS,
            minimum=1,
        ),
        raw_tool_prefixes=_coerce_raw_tool_prefixes(
            settings.get("raw_tool_prefixes", [])
        ),
    )


def estimate_billed_chars(messages: list[Message]) -> int:
    return sum(len(message.content) for message in messages)


def _is_direct_anthropic_model(model: str | None) -> bool:
    return bool(model and model.startswith(("anthropic/", "claude-")))


def _expected_cache_cold(costs: SessionCosts | None, model: str | None) -> bool:
    if not _is_direct_anthropic_model(model) or not costs:
        return False

    anthropic_entries = [
        entry for entry in costs.entries if _is_direct_anthropic_model(entry.model)
    ]
    if not anthropic_entries:
        return False

    if not any(entry.cache_creation_tokens > 0 for entry in anthropic_entries):
        return False

    last_timestamp = max(entry.timestamp for entry in anthropic_entries)
    return (time.time() - last_timestamp) > ANTHROPIC_CACHE_TTL_SECS


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


def _is_tool_output_message(
    message: Message, *, max_output_chars: int, raw_prefixes: tuple[str, ...]
) -> bool:
    if message.role != "system" or message.pinned:
        return False
    if message.content.startswith(TRIMMED_MARKER):
        return False
    # Skip messages already losslessly compressed by headroom-compressor
    if message.content.startswith(HEADROOM_MARKER):
        return False
    if len(message.content) <= max_output_chars:
        return False
    if not message.content.startswith(TOOL_OUTPUT_PREFIXES):
        return False
    if _content_matches_raw_prefix(message.content, raw_prefixes):
        return False
    return True


def _assistant_cutoff(messages: list[Message], recent_turns: int) -> int | None:
    assistant_positions = [
        index for index, message in enumerate(messages) if message.role == "assistant"
    ]
    if len(assistant_positions) <= recent_turns:
        return None
    return assistant_positions[-recent_turns]


def build_trimmed_content(content: str, preview_chars: int) -> str:
    preview = content[:preview_chars].rstrip()
    preview_len = min(len(content), preview_chars)
    return (
        f"[Tool output trimmed (orig={len(content)} chars); first {preview_len} chars]\n"
        f"{preview}"
    )


def apply_tool_output_trimmer(
    messages: list[Message],
    config: TrimmerConfig,
) -> tuple[list[Message], TrimSummary]:
    cutoff = _assistant_cutoff(messages, config.recent_turns)
    if cutoff is None:
        return list(messages), TrimSummary()

    rewritten: list[Message] = []
    trimmed_count = 0
    saved_chars = 0
    for index, message in enumerate(messages):
        if index < cutoff and _is_tool_output_message(
            message,
            max_output_chars=config.max_output_chars,
            raw_prefixes=config.raw_tool_prefixes,
        ):
            trimmed_content = build_trimmed_content(
                message.content, config.preview_chars
            )
            if len(trimmed_content) < len(message.content):
                rewritten.append(message.replace(content=trimmed_content))
                trimmed_count += 1
                saved_chars += len(message.content) - len(trimmed_content)
                continue
        rewritten.append(message)

    return rewritten, TrimSummary(trimmed_count=trimmed_count, saved_chars=saved_chars)


def determine_trigger(
    messages: list[Message],
    *,
    model: str | None,
    config: TrimmerConfig,
) -> TriggerDecision:
    try:
        from gptme.hooks.cache_awareness import (
            get_invalidation_count,
            get_turns_since_invalidation,
        )
    except ModuleNotFoundError:

        def get_invalidation_count() -> int:
            return 0

        def get_turns_since_invalidation() -> int:
            return 1

    return TriggerDecision(
        expected_cache_cold=_expected_cache_cold(
            CostTracker.get_session_costs(), model
        ),
        cache_invalidated=(
            get_invalidation_count() > 0 and get_turns_since_invalidation() == 0
        ),
        pressure_exceeded=estimate_billed_chars(messages) > config.pressure_chars,
    )


def _check_bypass_env() -> bool:
    """Check if the GPTME_TRIM_BYPASS env var is set, enabling full output pass-through."""
    val = os.environ.get(BYPASS_ENV_VAR, "").strip().lower()
    return val in ("1", "true", "yes")


def generation_pre_hook(
    messages: list[Message],
    **kwargs: Any,
) -> Generator[Message | StopPropagation, None, None]:
    """Trim old oversized tool output in-place before the LLM call."""
    config = get_trimmer_config()
    if not config.enabled:
        return

    # Explicit bypass: operator wants full output for current request.
    if _check_bypass_env():
        logger.info("tooloutput_trimmer: bypass active via %s", BYPASS_ENV_VAR)
        return

    trigger = determine_trigger(messages, model=kwargs.get("model"), config=config)
    if not trigger.active:
        return

    rewritten, summary = apply_tool_output_trimmer(messages, config)
    if summary.trimmed_count == 0:
        return

    messages[:] = rewritten
    logger.info(
        "tooloutput_trimmer: trimmed %d message(s), saved %d chars (%s)",
        summary.trimmed_count,
        summary.saved_chars,
        trigger.describe(),
    )
    yield from ()


def register() -> None:
    """Register read-time tool-output trimming hooks."""
    register_hook(
        name="tooloutput_trimmer.generation_pre",
        hook_type=HookType.GENERATION_PRE,
        func=generation_pre_hook,
        priority=200,
    )
