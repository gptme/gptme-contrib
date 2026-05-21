"""Regression tests for Discord bot tool-policy helpers."""

from __future__ import annotations

import ast
import contextvars
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).with_name("discord_bot.py")


class FakeLog:
    def __init__(self, messages):
        self.messages = list(messages)


class FakeLogManager:
    def __init__(self, messages):
        self.log = FakeLog(messages)

    @classmethod
    def load(cls, _logpath, initial_msgs, create=False):
        assert create is True
        return cls(initial_msgs)


class FakeLogger:
    def info(self, _message: str) -> None:
        return None


def _load_helpers(tmp_path: Path):
    tree = ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))
    selected_nodes: list[ast.stmt] = []
    names = {
        "DEFAULT_TOOL_ALLOWLIST",
        "default_tools",
        "load_tools_for_allowlist",
        "get_prompt_tools_for_allowlist",
        "get_conversation",
    }

    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in names:
                selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            selected_nodes.append(node)

    module = ast.Module(body=selected_nodes, type_ignores=[])

    events: list[object] = []
    current_tools: list[SimpleNamespace] = []
    captured_prompts: list[list[str]] = []

    def fake_clear_tools() -> None:
        events.append("clear")
        current_tools.clear()

    def fake_init_tools(allowlist: list[str]) -> None:
        events.append(("init", tuple(allowlist)))
        current_tools[:] = [SimpleNamespace(name=name) for name in allowlist]

    def fake_get_tools() -> list[SimpleNamespace]:
        events.append("get")
        return list(current_tools)

    def fake_get_prompt(*, tools):
        captured_prompts.append([tool.name for tool in tools])
        return [
            SimpleNamespace(
                role="system",
                content="tools:" + ",".join(tool.name for tool in tools),
            )
        ]

    namespace = {
        "ChannelID": int,
        "Log": FakeLog,
        "LogManager": FakeLogManager,
        "ToolSpec": object,
        "contextvars": contextvars,
        "clear_tools": fake_clear_tools,
        "copy": lambda items: list(items),
        "conversations": {},
        "default_tools": [],
        "get_prompt": fake_get_prompt,
        "get_tools": fake_get_tools,
        "init_tools": fake_init_tools,
        "logger": FakeLogger(),
        "logsdir": tmp_path,
    }
    exec(compile(module, str(SCRIPT_PATH), "exec"), namespace)
    return (
        namespace["load_tools_for_allowlist"],
        namespace["get_prompt_tools_for_allowlist"],
        namespace["get_conversation"],
        namespace["DEFAULT_TOOL_ALLOWLIST"],
        namespace,
        events,
        captured_prompts,
    )


def test_load_tools_for_allowlist_resets_context_before_init(tmp_path: Path) -> None:
    (
        load_tools_for_allowlist,
        _prompt_tools,
        _get_conversation,
        _default_allowlist,
        _ns,
        events,
        _captured,
    ) = _load_helpers(tmp_path)

    tools = load_tools_for_allowlist(("read", "shell"))

    assert [tool.name for tool in tools] == ["read", "shell"]
    assert events == ["clear", ("init", ("read", "shell")), "get"]


def test_get_prompt_tools_uses_default_snapshot_without_mutating_context(
    tmp_path: Path,
) -> None:
    (
        _load_tools,
        get_prompt_tools,
        _get_conversation,
        default_allowlist,
        namespace,
        events,
        _captured,
    ) = _load_helpers(tmp_path)
    namespace["default_tools"] = [
        SimpleNamespace(name="read"),
        SimpleNamespace(name="shell"),
    ]

    prompt_tools = get_prompt_tools(default_allowlist)

    assert [tool.name for tool in prompt_tools] == ["read", "shell"]
    assert events == []


def test_get_conversation_refreshes_stored_system_prompt_from_explicit_tools(
    tmp_path: Path,
) -> None:
    (
        _load_tools,
        _prompt_tools,
        get_conversation,
        _default_allowlist,
        _ns,
        _events,
        captured_prompts,
    ) = _load_helpers(tmp_path)

    first_tools = [SimpleNamespace(name="read"), SimpleNamespace(name="shell")]
    second_tools = [SimpleNamespace(name="read")]

    first_log = get_conversation(42, first_tools)
    second_log = get_conversation(42, second_tools)

    assert first_log.messages[0].content == "tools:read,shell"
    assert second_log.messages[0].content == "tools:read"
    assert _ns["conversations"][42].log.messages[0].content == "tools:read"
    assert captured_prompts == [["read", "shell"], ["read"]]
