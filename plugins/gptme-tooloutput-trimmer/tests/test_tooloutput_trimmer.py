from __future__ import annotations

import os
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
    BYPASS_ENV_VAR,
    TRIMMED_MARKER,
    TrimmerConfig,
    apply_tool_output_trimmer,
    build_trimmed_content,
    generation_pre_hook,
    get_trimmer_config,
    reset_state,
)
from tooloutput_trimmer.hooks.trimmer import _is_tool_output_message  # noqa: E402


def _msg(role: str, content: str, **kwargs: object) -> Message:
    return Message(role, content, timestamp=datetime.now(timezone.utc), **kwargs)


def _make_config(
    *, enabled: bool = True, plugin_settings: dict[str, object] | None = None
) -> SimpleNamespace:
    plugin_cfg = {
        "tooloutput_trimmer": {
            "max_output_chars": 200,
            "recent_turns": 1,
            "preview_chars": 40,
            "pressure_chars": 1_000,
        }
    }
    if plugin_settings:
        plugin_cfg["tooloutput_trimmer"].update(plugin_settings)
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


def test_is_tool_output_message_respects_raw_prefixes() -> None:
    """Messages matching raw_tool_prefixes should not be trimmed."""
    content = _big_shell_output("cat config.py", repeats=300)
    msg = _msg("system", content)

    # With no raw_prefixes, should be eligible.
    assert _is_tool_output_message(msg, max_output_chars=200, raw_prefixes=())

    # With a matching raw_prefix, should be excluded.
    assert not _is_tool_output_message(
        msg, max_output_chars=200, raw_prefixes=("cat ",)
    )

    # With a non-matching raw_prefix, should still be eligible.
    assert _is_tool_output_message(msg, max_output_chars=200, raw_prefixes=("diff ",))


def test_get_trimmer_config_coerces_single_raw_prefix_string() -> None:
    with patch(
        "tooloutput_trimmer.hooks.trimmer.get_config",
        return_value=_make_config(plugin_settings={"raw_tool_prefixes": "cat "}),
    ):
        config = get_trimmer_config()

    assert config.raw_tool_prefixes == ("cat ",)


def test_is_tool_output_message_keeps_code_block_outputs_eligible() -> None:
    content = "Executed code block.\n\n```stdout\nhello\n```\n" + ("abc" * 100)
    msg = _msg("system", content)

    # Python tool outputs do not retain the original code string, so
    # raw_tool_prefixes cannot exempt them.
    assert _is_tool_output_message(
        msg,
        max_output_chars=50,
        raw_prefixes=("print(",),
    )


def test_apply_tool_output_trimmer_respects_raw_prefixes() -> None:
    """Commands matching raw_tool_prefixes should bypass trimming entirely."""
    cat_output = _big_shell_output("cat config.py", repeats=300)
    ls_output = _big_shell_output("ls -la", repeats=300)
    # Stage messages so both tool outputs are old enough to be eligible for
    # trimming (index < cutoff where cutoff = assistant_positions[-recent_turns]).
    # cat (idx 2) matches raw_prefixes → preserved; ls (idx 4) does not → trimmed.
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", cat_output),
        _msg("user", "u1"),
        _msg("system", ls_output),
        _msg("assistant", "a1"),
        _msg("user", "u2"),
        _msg("assistant", "a2"),
    ]

    rewritten, summary = apply_tool_output_trimmer(
        messages,
        TrimmerConfig(
            enabled=True,
            max_output_chars=200,
            recent_turns=2,
            preview_chars=40,
            pressure_chars=10_000,
            raw_tool_prefixes=("cat ",),
        ),
    )

    # cat output should be preserved (raw-prefix exempt), ls output trimmed.
    assert summary.trimmed_count == 1
    assert rewritten[2].content == cat_output
    assert rewritten[4].content.startswith(TRIMMED_MARKER)


def test_generation_pre_hook_bypasses_on_env_var() -> None:
    """When GPTME_TRIM_BYPASS=1, no trimming should occur."""
    reset_state()
    original = _big_shell_output(repeats=300)
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", original),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("user", "please continue"),
    ]

    with patch.dict(os.environ, {BYPASS_ENV_VAR: "1"}, clear=False):
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

    assert messages[2].content == original


def test_generation_pre_hook_noop_with_bypass_env_false() -> None:
    """When GPTME_TRIM_BYPASS=0, trimming should proceed normally."""
    reset_state()
    messages = [
        _msg("user", "u0"),
        _msg("assistant", "a0"),
        _msg("system", _big_shell_output(repeats=250)),
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _msg("user", "please continue"),
    ]

    with patch.dict(os.environ, {BYPASS_ENV_VAR: "0"}, clear=False):
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
