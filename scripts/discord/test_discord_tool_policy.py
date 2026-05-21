"""Regression tests for Discord bot tool-policy helpers."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncGenerator

import pytest

SCRIPT_PATH = Path(__file__).with_name("discord_bot.py")


class FakeLog:
    def __init__(self, messages):
        self.messages = list(messages)

    def __len__(self):
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def append(self, message):
        return type(self)(self.messages + [message])

    def replace(self, *, messages):
        return type(self)(messages)


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
        "get_prompt_tools_for_allowlist_async",
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
        elif isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            selected_nodes.append(node)

    module = ast.Module(body=selected_nodes, type_ignores=[])

    events: list[object] = []
    current_tools: list[SimpleNamespace] = []
    captured_prompts: list[list[str]] = []
    tool_threads: list[int] = []

    def fake_clear_tools() -> None:
        events.append("clear")
        tool_threads.append(threading.get_ident())
        current_tools.clear()

    def fake_init_tools(allowlist: list[str]) -> None:
        events.append(("init", tuple(allowlist)))
        tool_threads.append(threading.get_ident())
        current_tools[:] = [SimpleNamespace(name=name) for name in allowlist]

    def fake_get_tools() -> list[SimpleNamespace]:
        events.append("get")
        tool_threads.append(threading.get_ident())
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
        "asyncio": asyncio,
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
        namespace["get_prompt_tools_for_allowlist_async"],
        namespace["get_conversation"],
        namespace["DEFAULT_TOOL_ALLOWLIST"],
        namespace,
        events,
        captured_prompts,
        tool_threads,
    )


def _load_async_step(tmp_path: Path):
    tree = ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))
    async_step_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step"
    )
    module = ast.Module(body=[async_step_node], type_ignores=[])

    step_calls: list[list[str]] = []
    tool_load_calls: list[tuple[str, ...]] = []
    completion_events: list[dict[str, object]] = []

    class FakeOperation:
        def complete(self, **kwargs) -> None:
            completion_events.append(kwargs)

    class FakeMetrics:
        def start_operation(self, *_args) -> FakeOperation:
            return FakeOperation()

    class FakeToolUse:
        @staticmethod
        def iter_from_content(content: str):
            if content == "tool:run":
                return [SimpleNamespace(is_runnable=True)]
            return []

    class FakeContext:
        def run(self, fn, *args):
            return fn(*args)

    class FakeLogger:
        def exception(self, _message: str) -> None:
            return None

    def fake_step(current_log, **_kwargs):
        step_calls.append([msg.content for msg in current_log.messages])
        if len(step_calls) == 1:
            return [SimpleNamespace(role="assistant", content="tool:run")]
        return [SimpleNamespace(role="assistant", content="done")]

    async def fake_fetch_discord_history(_channel) -> str:
        return ""

    def fake_load_tools_for_allowlist(tool_allowlist: tuple[str, ...]):
        tool_load_calls.append(tool_allowlist)
        return [SimpleNamespace(name=name) for name in tool_allowlist]

    namespace = {
        "AsyncGenerator": AsyncGenerator,
        "Log": FakeLog,
        "Message": SimpleNamespace,
        "ToolUse": FakeToolUse,
        "asyncio": asyncio,
        "contextvars": SimpleNamespace(copy_context=lambda: FakeContext()),
        "discord": SimpleNamespace(abc=SimpleNamespace(Messageable=object)),
        "fetch_discord_history": fake_fetch_discord_history,
        "get_settings": lambda _channel_id: SimpleNamespace(model="test-model"),
        "load_tools_for_allowlist": fake_load_tools_for_allowlist,
        "logger": FakeLogger(),
        "metrics": FakeMetrics(),
        "step": fake_step,
        "workspace_root": tmp_path,
    }
    exec(compile(module, str(SCRIPT_PATH), "exec"), namespace)
    return namespace["async_step"], step_calls, tool_load_calls, completion_events


def test_load_tools_for_allowlist_resets_context_before_init(tmp_path: Path) -> None:
    (
        load_tools_for_allowlist,
        _prompt_tools,
        _prompt_tools_async,
        _get_conversation,
        _default_allowlist,
        _ns,
        events,
        _captured,
        _tool_threads,
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
        _prompt_tools_async,
        _get_conversation,
        default_allowlist,
        namespace,
        events,
        _captured,
        _tool_threads,
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
        _prompt_tools_async,
        get_conversation,
        _default_allowlist,
        _ns,
        _events,
        captured_prompts,
        _tool_threads,
    ) = _load_helpers(tmp_path)

    first_tools = [SimpleNamespace(name="read"), SimpleNamespace(name="shell")]
    second_tools = [SimpleNamespace(name="read")]

    first_log = get_conversation(42, first_tools)
    second_log = get_conversation(42, second_tools)

    assert first_log.messages[0].content == "tools:read,shell"
    assert second_log.messages[0].content == "tools:read"
    assert _ns["conversations"][42].log.messages[0].content == "tools:read"
    assert captured_prompts == [["read", "shell"], ["read"]]


@pytest.mark.asyncio
async def test_get_prompt_tools_async_offloads_non_default_allowlist(
    tmp_path: Path,
) -> None:
    (
        _load_tools,
        _prompt_tools,
        get_prompt_tools_async,
        _get_conversation,
        _default_allowlist,
        _ns,
        events,
        _captured,
        tool_threads,
    ) = _load_helpers(tmp_path)

    main_thread_id = threading.get_ident()

    prompt_tools = await get_prompt_tools_async(("read",))

    assert [tool.name for tool in prompt_tools] == ["read"]
    assert events == ["clear", ("init", ("read",)), "get"]
    assert tool_threads
    assert any(thread_id != main_thread_id for thread_id in tool_threads)


@pytest.mark.asyncio
async def test_async_step_reloads_tools_once_per_request(tmp_path: Path) -> None:
    async_step, step_calls, tool_load_calls, completion_events = _load_async_step(
        tmp_path
    )

    log = FakeLog([SimpleNamespace(role="user", content="hello")])
    messages = [
        msg
        async for msg in async_step(
            log,
            42,
            SimpleNamespace(),
            ("read", "shell"),
        )
    ]

    assert [msg.content for msg in messages] == ["tool:run", "done"]
    assert step_calls == [["hello"], ["hello", "tool:run"]]
    assert tool_load_calls == [("read", "shell")]
    assert completion_events == [{"success": True}]
