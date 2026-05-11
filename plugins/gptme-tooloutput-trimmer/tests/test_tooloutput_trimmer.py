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

from tooloutput_trimmer.hooks import (  # noqa: E402
    TRIMMED_MARKER,
    TrimmerConfig,
    apply_tool_output_trimmer,
    build_trimmed_content,
    generation_pre_hook,
    reset_state,
)


def _msg(role: str, content: str, **kwargs: object) -> Message:
    return Message(role, content, timestamp=datetime.now(timezone.utc), **kwargs)


def _make_config(*, enabled: bool = True) -> SimpleNamespace:
    plugin_cfg = {
        "tooloutput_trimmer": {
            "max_output_chars": 200,
            "recent_turns": 1,
            "preview_chars": 40,
            "pressure_chars": 1_000,
        }
    }
    return SimpleNamespace(
        get_env_bool=lambda key: enabled if key == "GPTME_READ_TIME_TRIMMER" else None,
        user=SimpleNamespace(plugin={}),
        project=SimpleNamespace(plugin=plugin_cfg),
    )


def _big_shell_output(name: str = "ls", repeats: int = 200) -> str:
    body = "\n".join(f"line-{i}" for i in range(repeats))
    return f"Ran command: `{name}`\n{body}"


def test_apply_tool_output_trimmer_preserves_recent_turns() -> None:
    old_output = _big_shell_output("old", repeats=220)
    recent_output = _big_shell_output("recent", repeats=220)
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", old_output),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("system", recent_output),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
    ]

    rewritten, summary = apply_tool_output_trimmer(
        messages,
        TrimmerConfig(
            enabled=True,
            max_output_chars=200,
            recent_turns=2,
            preview_chars=60,
            pressure_chars=10_000,
        ),
    )

    assert summary.trimmed_count == 1
    assert rewritten[2].content.startswith(TRIMMED_MARKER)
    assert rewritten[5].content == recent_output


def test_apply_tool_output_trimmer_ignores_non_shell_system_messages() -> None:
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", "Saved to `x.py`\n" + ("x" * 400)),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
    ]

    rewritten, summary = apply_tool_output_trimmer(
        messages,
        TrimmerConfig(
            enabled=True,
            max_output_chars=200,
            recent_turns=1,
            preview_chars=40,
            pressure_chars=10_000,
        ),
    )

    assert summary.trimmed_count == 0
    assert rewritten[2].content.startswith("Saved to `x.py`")


def test_generation_pre_hook_trims_on_context_pressure() -> None:
    reset_state()
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output(repeats=250)),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("user", "please continue"),
    ]

    with patch(
        "tooloutput_trimmer.hooks.trimmer.get_config", return_value=_make_config()
    ):
        with patch(
            "tooloutput_trimmer.hooks.trimmer.determine_trigger",
            return_value=SimpleNamespace(
                active=True,
                describe=lambda: "context-pressure",
            ),
        ):
            list(generation_pre_hook(messages, model="openai/gpt-4o"))

    assert messages[2].content.startswith(TRIMMED_MARKER)


def test_generation_pre_hook_noops_when_disabled() -> None:
    reset_state()
    original = _big_shell_output(repeats=250)
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", original),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("user", "please continue"),
    ]

    with patch(
        "tooloutput_trimmer.hooks.trimmer.get_config",
        return_value=_make_config(enabled=False),
    ):
        list(generation_pre_hook(messages, model="openai/gpt-4o"))

    assert messages[2].content == original


def test_build_trimmed_content_keeps_preview_prefix() -> None:
    content = "Executed code block.\n\n" + ("abc" * 100)
    trimmed = build_trimmed_content(content, 25)
    assert trimmed.startswith(TRIMMED_MARKER)
    assert "Executed code block." in trimmed
