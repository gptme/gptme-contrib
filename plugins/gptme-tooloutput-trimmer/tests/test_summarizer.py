"""Tests for the tool-output summarization pass."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gptme.message import Message

# Add plugin source to path once at module load.
_plugin_src = str(Path(__file__).parent.parent / "src")
if _plugin_src not in sys.path:
    sys.path.insert(0, _plugin_src)

import pytest  # noqa: E402
from tooloutput_trimmer.hooks.summarizer import (  # noqa: E402
    _build_summarization_context,
    _call_summarizer,
    _find_evictable_tool_output_indices,
    apply_summarization,
    generation_pre_hook,
    get_summarizer_config,
)
from tooloutput_trimmer.hooks.trimmer import (  # noqa: E402
    SUMMARIZATION_MARKER,
    TrimmerConfig,
)


def _msg(role: str, content: str, **kwargs: object) -> Message:
    return Message(role, content, timestamp=datetime.now(timezone.utc), **kwargs)


def _big_shell_output(name: str = "ls", repeats: int = 200) -> str:
    body = "\n".join(f"line-{i}" for i in range(repeats))
    return f"Ran command: `{name}`\n{body}"


def _make_config(
    *,
    enabled: bool = True,
    summarize_enabled: bool = False,
    plugin_settings: dict[str, object] | None = None,
) -> SimpleNamespace:
    plugin_cfg: dict[str, object] = {
        "tooloutput_trimmer": {
            "max_output_chars": 200,
            "recent_turns": 1,
            "preview_chars": 40,
            "pressure_chars": 1_000,
        }
    }
    if summarize_enabled:
        plugin_cfg["tooloutput_trimmer"]["summarize"] = True  # type: ignore[assignment]
    if plugin_settings:
        plugin_cfg["tooloutput_trimmer"].update(plugin_settings)  # type: ignore[assignment]
    return SimpleNamespace(
        get_env_bool=lambda key: (
            enabled if key == "GPTME_READ_TIME_TRIMMER" else None
        ),
        user=SimpleNamespace(plugin={}),
        project=SimpleNamespace(plugin=plugin_cfg),
    )


def test_find_evictable_finds_old_tool_outputs() -> None:
    """Evictable indices should include tool outputs before recency cutoff."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("old1")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("system", _big_shell_output("old2")),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
    ]
    config = TrimmerConfig(
        enabled=True,
        max_output_chars=200,
        recent_turns=1,
        preview_chars=40,
        pressure_chars=10_000,
    )
    evictable = _find_evictable_tool_output_indices(messages, config)
    # Only 1 recent turn means cutoff is at the last assistant position.
    # old1 (idx 2) and old2 (idx 5) are before that cutoff.
    assert 2 in evictable
    assert 5 in evictable


def test_find_evictable_empty_when_no_cutoff() -> None:
    """When messages fit within recency window, nothing is evictable."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("recent")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]
    config = TrimmerConfig(
        enabled=True,
        max_output_chars=200,
        recent_turns=5,  # larger than assistant count
        preview_chars=40,
        pressure_chars=10_000,
    )
    assert _find_evictable_tool_output_indices(messages, config) == []


def _default_trimmer_config(
    recent_turns: int = 1,
) -> TrimmerConfig:
    return TrimmerConfig(
        enabled=True,
        max_output_chars=200,
        recent_turns=recent_turns,
        preview_chars=40,
        pressure_chars=10_000,
    )


def test_apply_summarization_noop_when_disabled() -> None:
    """When summarization is disabled, no changes are made."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("old")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]
    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_config",
        return_value=_make_config(summarize_enabled=False),
    ):
        rewritten, did_summarize = apply_summarization(
            messages,
            trimmer_config=_default_trimmer_config(),
        )
    assert not did_summarize
    assert len(rewritten) == len(messages)


def test_apply_summarization_skips_when_summarizer_fails() -> None:
    """When the summarizer LLM call fails, fall back to no summarization."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("old")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]

    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_default_model_summary",
        return_value=None,
    ):
        with patch(
            "tooloutput_trimmer.hooks.summarizer.get_config",
            return_value=_make_config(summarize_enabled=True),
        ):
            rewritten, did_summarize = apply_summarization(
                messages,
                trimmer_config=_default_trimmer_config(),
            )

    assert not did_summarize
    assert len(rewritten) == len(messages)


def test_apply_summarization_replaces_evicted_pairs_with_summary() -> None:
    """When summarization succeeds, W evicted pairs are replaced with a summary
    message."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("old1")),
        _msg("user", "u1"),
        _msg("system", _big_shell_output("old2")),
        _msg("assistant", "a1"),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
    ]

    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_default_model_summary",
        return_value=SimpleNamespace(full="openai/gpt-4o"),
    ):
        with patch(
            "tooloutput_trimmer.hooks.summarizer._call_summarizer",
            return_value="- Executed command: found result\n- Task-level progress: done",
        ):
            with patch(
                "tooloutput_trimmer.hooks.summarizer.get_config",
                return_value=_make_config(summarize_enabled=True),
            ):
                rewritten, did_summarize = apply_summarization(
                    messages,
                    trimmer_config=_default_trimmer_config(),
                )

    assert did_summarize
    # Evicted pairs at idx 2 and idx 4 → replaced with 1 summary at idx 2
    # New order: idx 2 = summary, idx 3 = u1, idx 4 = a1 (was idx 5),
    # idx 5 = u2 (was idx 6), idx 6 = a2 (was idx 7) = total 7
    assert len(rewritten) == len(messages) - 1

    # The summary message is at position 2 (the first evicted position)
    assert rewritten[2].content.startswith(SUMMARIZATION_MARKER)
    assert "Executed command" in rewritten[2].content

    # Recent messages should be unaffected
    assert rewritten[4].content == "a1"  # a1, was at idx 5, shifted to idx 4
    assert rewritten[5].content == "u2"  # u2, was at idx 6, shifted to idx 5
    assert rewritten[6].content == "a2"  # a2, was at idx 7, shifted to idx 6


def test_apply_summarization_noop_when_no_evictable_pairs() -> None:
    """When no tool outputs are beyond the recency window, no-op."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("recent")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]
    # Use recent_turns=3 to keep the single tool output within the window
    tc = _default_trimmer_config(recent_turns=3)

    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_default_model_summary",
        return_value=SimpleNamespace(full="openai/gpt-4o"),
    ):
        with patch(
            "tooloutput_trimmer.hooks.summarizer.get_config",
            return_value=_make_config(summarize_enabled=True),
        ):
            rewritten, did_summarize = apply_summarization(
                messages,
                trimmer_config=tc,
            )

    assert not did_summarize
    assert len(rewritten) == len(messages)


def test_build_summarization_context_includes_tool_call_and_output() -> None:
    """Context should include the preceding assistant message and tool output."""
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "check files"),
        _msg("system", _big_shell_output("ls", repeats=210)),
        _msg("user", "u1"),
        _msg("assistant", "read config"),
        _msg("system", _big_shell_output("cat config.py", repeats=210)),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
    ]
    config = TrimmerConfig(
        enabled=True,
        max_output_chars=200,
        recent_turns=1,
        preview_chars=40,
        pressure_chars=10_000,
    )
    evictable = _find_evictable_tool_output_indices(messages, config)
    context = _build_summarization_context(messages, evictable, window=2)

    assert "Tool call: check files" in context
    assert "Tool call: read config" in context
    assert "Tool output: Ran command: `ls`" in context
    assert "Tool output: Ran command: `cat config.py`" in context


def test_call_summarizer_returns_none_on_failure() -> None:
    """When the summarizer LLM call fails, return None."""
    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_default_model_summary",
        return_value=SimpleNamespace(full="openai/gpt-4o"),
    ):
        with patch(
            "tooloutput_trimmer.hooks.summarizer._chat_complete",
            side_effect=RuntimeError("API error"),
        ):
            result = _call_summarizer("some context")
    assert result is None


def test_get_summarizer_config_defaults() -> None:
    """Defaults should be disabled with window=3."""
    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_config",
        return_value=SimpleNamespace(
            get_env_bool=lambda key: None,
            user=SimpleNamespace(plugin={}),
            project=SimpleNamespace(plugin={}),
        ),
    ):
        config = get_summarizer_config()

    assert config.enabled is False
    assert config.window == 3


def test_get_summarizer_config_from_env() -> None:
    """GPTME_SUMMARIZE_TOOL_OUTPUTS env var should enable summarization."""
    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_config",
        return_value=SimpleNamespace(
            get_env_bool=lambda key: (
                True if key == "GPTME_SUMMARIZE_TOOL_OUTPUTS" else None
            ),
            user=SimpleNamespace(plugin={}),
            project=SimpleNamespace(plugin={}),
        ),
    ):
        config = get_summarizer_config()

    assert config.enabled is True


def test_apply_summarization_respects_window_limit() -> None:
    """Only W most recent evicted pairs should be summarized, not all."""
    # Create messages with 4 evictable tool outputs
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("old1")),
        _msg("user", "u1"),
        _msg("system", _big_shell_output("old2")),
        _msg("user", "u2"),
        _msg("system", _big_shell_output("old3")),
        _msg("user", "u3"),
        _msg("system", _big_shell_output("old4")),
        _msg("assistant", "a1"),
        _msg("user", "u4"),
        _msg("assistant", "a2"),
    ]

    with patch(
        "tooloutput_trimmer.hooks.summarizer.get_default_model_summary",
        return_value=SimpleNamespace(full="openai/gpt-4o"),
    ):
        with patch(
            "tooloutput_trimmer.hooks.summarizer._call_summarizer",
            return_value="- Summary of latest evicted",
        ):
            with patch(
                "tooloutput_trimmer.hooks.summarizer.get_config",
                return_value=_make_config(summarize_enabled=True),
            ):
                rewritten, did_summarize = apply_summarization(
                    messages,
                    trimmer_config=_default_trimmer_config(),
                )

    assert did_summarize
    # W=3, evicted=[2,4,6,8], recent_evicted=[4,6,8] (3 most recent)
    # Indices 4,6,8 → replaced with 1 summary at idx 4
    # Net: -2 messages (12 → 10)
    # Oldest evicted pair (idx 2) stays as-is
    assert len(rewritten) == len(messages) - 2

    # Summary should be at position 4 (the first of the 3 evicted recent pairs)
    assert rewritten[4].content.startswith(SUMMARIZATION_MARKER)

    # The oldest evicted pair (idx 2) should still be there as-is
    assert rewritten[2].content == messages[2].content


def test_generation_pre_hook_noop_when_bypass_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GPTME_TRIM_BYPASS is set the summarizer hook must not fire."""
    monkeypatch.setenv("GPTME_TRIM_BYPASS", "1")

    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output("bypass-test")),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]
    original = list(messages)

    # generation_pre_hook is a generator; exhaust it
    list(generation_pre_hook(messages, model="test-model"))

    # messages must be unchanged — no LLM call, no summarization
    assert messages == original
