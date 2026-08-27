"""Summarization pass for evicted tool outputs.

Runs at priority 202 (before the headroom compressor at 201 and the trimmer at
200, all of which use higher-priority-runs-first ordering). When summarization is enabled
and tool output messages are evicted beyond the recency window, the W most
recently evicted pairs are summarized using an LLM and replaced with a single
summary message, instead of being preview-truncated by the trimmer.

Based on Algorithm 1 from "Less Context, Better Agents" (arXiv:2606.10209,
Lodha et al., Microsoft, 2026). Summarization adds ~+12.6pp reliability over
pruning alone at ~+3.4% token cost.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from gptme.config import get_config
from gptme.hooks import HookType, StopPropagation, register_hook
from gptme.message import Message

try:
    from gptme.llm import _chat_complete, get_default_model_summary
except ModuleNotFoundError:

    def _chat_complete(
        messages: list[Message], model: str, tools: Any = None, **kwargs: Any
    ) -> tuple[str, Any]:
        raise RuntimeError("LLM summarization not available (old gptme)")

    def get_default_model_summary() -> Any:
        return None


from .trimmer import (
    SUMMARIZATION_MARKER,
    SUMMARIZE_ENV_VAR,
    TrimmerConfig,
    _check_bypass_env,
    _coerce_int,
    _is_tool_output_message,
    get_trimmer_config,
)


def _get_plugin_settings() -> dict[str, Any]:
    """Read tooloutput_trimmer plugin settings, merging user and project config.

    Defined locally (not imported from trimmer) so that patching
    `summarizer.get_config` in tests correctly intercepts this lookup.
    """
    config = get_config()
    user_cfg: dict[str, Any] = {}
    project_cfg: dict[str, Any] = {}
    if config.user and hasattr(config.user, "plugin"):
        user_cfg = getattr(config.user, "plugin", {}).get("tooloutput_trimmer", {})
    if config.project and hasattr(config.project, "plugin"):
        project_cfg = getattr(config.project, "plugin", {}).get(
            "tooloutput_trimmer", {}
        )
    return {**user_cfg, **project_cfg}


logger = logging.getLogger(__name__)

# Default summarization settings
DEFAULT_SUMMARIZATION_ENABLED = False  # off by default for MVP
DEFAULT_SUMMARIZATION_WINDOW = 3  # W=3 matches paper's optimum


SUMMARIZATION_PROMPT = """Summarize the following tool execution results for an AI assistant. Focus on what commands were executed, what key results were discovered or created, and what task-level progress was made.

Tool outputs to summarize:
{context}

Format:
Summary of previous tool calls:
- [action]: [result]
- [action]: [result]
- Task-level progress: [what's been done, what's pending]"""

# Bounded content-addressable memo of LLM-produced summaries. The source is the
# summarization context string plus the model identity; an unchanged source and
# model reuse the cached summary so repeated preparation passes over the same
# stored log do not re-bill the summarizer LLM (Phase 1.2 of the append-only
# request-construction audit). The key uses a SHA-256 of the context so a large
# tool-output blob is not held in memory and membership is O(1).
_SUMMARY_CACHE: dict[tuple[str, str], str] = {}
_SUMMARY_CACHE_MAX = 64
_SUMMARY_CACHE_LOCK = threading.Lock()


def _cache_summary(context: str, model: str, summary: str) -> None:
    """Store a summary under (context-hash, model), bounding cache size."""
    key = (hashlib.sha256(context.encode()).hexdigest(), model)
    with _SUMMARY_CACHE_LOCK:
        if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX and key not in _SUMMARY_CACHE:
            # Evict an arbitrary entry to keep the cache bounded. This is a plain
            # memo, not an LRU: correctness is unaffected by which entry goes.
            _SUMMARY_CACHE.pop(next(iter(_SUMMARY_CACHE)))
        _SUMMARY_CACHE[key] = summary


def _cached_summary(context: str, model: str) -> str | None:
    """Return a cached summary for (context-hash, model), or None."""
    key = (hashlib.sha256(context.encode()).hexdigest(), model)
    with _SUMMARY_CACHE_LOCK:
        return _SUMMARY_CACHE.get(key)


@dataclass
class SummarizerConfig:
    """Configuration for the summarization pass."""

    enabled: bool = False
    window: int = (
        DEFAULT_SUMMARIZATION_WINDOW  # W — number of evicted pairs to summarize
    )


def _get_summarization_enabled() -> bool:
    """Check if summarization is enabled via env var or plugin config."""
    settings = _get_plugin_settings()
    config = get_config()
    env_val = config.get_env_bool(SUMMARIZE_ENV_VAR)
    if env_val is not None:
        return env_val
    return bool(settings.get("summarize", DEFAULT_SUMMARIZATION_ENABLED))


def get_summarizer_config() -> SummarizerConfig:
    """Read summarizer config from env + plugin settings."""
    return SummarizerConfig(
        enabled=_get_summarization_enabled(),
        window=_coerce_int(
            _get_plugin_settings().get("summarize_window"),
            DEFAULT_SUMMARIZATION_WINDOW,
            minimum=1,
        ),
    )


def _find_evictable_tool_output_indices(
    messages: list[Message],
    trimmer_config: TrimmerConfig,
) -> list[int]:
    """Find indices of tool output messages beyond the recency window.

    Uses the same cutoff logic as the trimmer: messages before the Nth-last
    assistant turn are eligible for eviction/summarization.
    """
    assistant_positions = [
        index for index, message in enumerate(messages) if message.role == "assistant"
    ]
    if len(assistant_positions) <= trimmer_config.recent_turns:
        return []

    cutoff = assistant_positions[-trimmer_config.recent_turns]

    evictable: list[int] = []
    for index in range(cutoff):
        if _is_tool_output_message(
            messages[index],
            max_output_chars=trimmer_config.max_output_chars,
            raw_prefixes=trimmer_config.raw_tool_prefixes,
        ):
            evictable.append(index)

    return evictable


def _build_summarization_context(
    messages: list[Message],
    evictable_indices: list[int],
    window: int,
) -> str:
    """Build context string from the W most recently evicted pairs.

    Includes the preceding assistant message (tool call) and the tool output
    for each evicted pair, truncated to a reasonable size for summarization.
    """
    # Take the W most recently evicted pairs (last in the list = closest to present)
    recent_evicted = evictable_indices[-window:]

    parts: list[str] = []
    for idx in recent_evicted:
        # Include preceding assistant message if available
        if idx > 0 and messages[idx - 1].role == "assistant":
            call_preview = messages[idx - 1].content[:300].replace("\n", " ")
            parts.append(f"Tool call: {call_preview}")
        # Include first 1000 chars of the tool output
        output_preview = messages[idx].content[:1000].replace("\n", " ")
        parts.append(f"Tool output: {output_preview}")

    return "\n\n".join(parts)


def _call_summarizer(context: str) -> str | None:
    """Produce a summary of the given context, memoized by source and model.

    Returns the summary text, or None if summarization fails. An unchanged
    (context, model) pair reuses the cached summary so repeated preparation
    passes over the same stored log do not re-bill the summarizer LLM.

    The cache is keyed on the truncated context string actually sent to the LLM,
    not the full message content. This ensures that identical prompts (e.g., a
    log file that grows beyond the 1000-char truncation boundary) reuse the cached
    summary, improving performance for repeated passes over stored logs.
    """
    model = get_default_model_summary()
    if not model:
        logger.warning("summarizer: no default model set, skipping")
        return None

    cached = _cached_summary(context, model.full)
    if cached is not None:
        logger.debug("summarizer: using cached summary (%d chars)", len(cached))
        return cached

    prompt = SUMMARIZATION_PROMPT.format(context=context)
    msgs = [
        Message(
            "system",
            content="You are a helpful assistant that summarizes tool execution results concisely.",
        ),
        Message("user", content=prompt),
    ]

    try:
        summary, _metadata = _chat_complete(msgs, model.full, None)
        if not summary:
            logger.warning("summarizer: LLM returned empty summary")
            return None
        summary = summary.strip()
        if not summary:
            logger.warning("summarizer: LLM returned whitespace-only summary")
            return None
        logger.debug(
            "summarizer: produced %d chars",
            len(summary),
        )
        _cache_summary(context, model.full, summary)
        return summary
    except Exception:
        logger.exception("summarizer: LLM call failed")
        return None


def apply_summarization(
    messages: list[Message],
    *,
    trimmer_config: TrimmerConfig | None = None,
) -> tuple[list[Message], bool]:
    """Apply summarization to evictable tool output pairs.

    Returns (rewritten_messages, did_summarize) where did_summarize is True
    if any summarization was performed.

    Can optionally pass a TrimmerConfig to use for eviction detection
    (useful in tests). Otherwise reads from plugin config.
    """
    config = get_summarizer_config()
    if not config.enabled:
        return list(messages), False

    trimmer_config = trimmer_config or get_trimmer_config()
    evictable = _find_evictable_tool_output_indices(messages, trimmer_config)

    if not evictable:
        return list(messages), False

    # Build context from the W most recently evicted pairs (truncated for LLM prompt).
    # This truncated context is also used as the cache key, ensuring that identical
    # prompts (regardless of un-truncated content changes) reuse cached summaries.
    context = _build_summarization_context(messages, evictable, config.window)

    # Call summarizer with context-based memoization
    summary = _call_summarizer(context)
    if summary is None:
        return list(messages), False  # fall back to preview truncation

    # Take the W most recently evicted pairs
    recent_evicted = evictable[-config.window :]
    # Build the summary message content
    summary_content = f"{SUMMARIZATION_MARKER}\n{summary}"
    summary_msg = messages[recent_evicted[0]].replace(content=summary_content)

    # Rebuild message list: replace the first evicted position with summary,
    # remove the rest
    evicted_set = set(recent_evicted)
    rewritten: list[Message] = []
    for idx, msg in enumerate(messages):
        if idx == recent_evicted[0]:
            rewritten.append(summary_msg)
        elif idx in evicted_set:
            continue  # skip other evicted positions
        else:
            rewritten.append(msg)

    return rewritten, True


def generation_pre_hook(
    messages: list[Message],
    **kwargs: Any,
) -> Generator[Message | StopPropagation, None, None]:
    """Summarize evicted tool outputs before the trimmer runs.

    Registered at priority 202 (runs before the headroom compressor at 201 and
    the trimmer at 200). When summarization is enabled, replaces W evicted tool
    output pairs with a single LLM-generated summary. Otherwise, acts as a no-op
    and the trimmer handles truncation.
    """
    if _check_bypass_env():
        return
    # Capture count before in-place update so the scan sees the original list
    n_evictable = len(
        _find_evictable_tool_output_indices(messages, get_trimmer_config())
    )
    rewritten, did_summarize = apply_summarization(messages)
    if did_summarize:
        messages[:] = rewritten
        logger.info(
            "summarizer: replaced %d evicted pairs with summary",
            min(get_summarizer_config().window, n_evictable),
        )
    yield from ()


def register() -> None:
    """Register the summarization hook at priority 202 (runs before the headroom
    compressor at 201 and the trimmer at 200)."""
    register_hook(
        name="tooloutput_trimmer.summarizer",
        hook_type=HookType.GENERATION_PRE,
        func=generation_pre_hook,
        priority=202,
    )
