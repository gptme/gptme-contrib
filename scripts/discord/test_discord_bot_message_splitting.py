"""Regression tests for Discord message splitting logic."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).with_name("discord_bot.py")


def _load_helpers():
    tree = ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))
    selected_nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DISCORD_MSG_LIMIT"
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "split_on_codeblocks":
            selected_nodes.append(node)
        elif (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "send_discord_message"
        ):
            selected_nodes.append(node)

    module = ast.Module(body=selected_nodes, type_ignores=[])

    class FakeHTTPException(Exception):
        """Stand-in for discord.HTTPException."""

    class FakeOp:
        def __init__(self) -> None:
            self.success: bool | None = None
            self.error: str | None = None

        def complete(self, success: bool, error: str | None = None) -> None:
            self.success = success
            self.error = error

    class FakeMetrics:
        def start_operation(self, *_args, **_kwargs) -> FakeOp:
            return FakeOp()

    class FakeLogger:
        def info(self, _message: str) -> None:
            return None

        def warning(self, _message: str) -> None:
            return None

        def error(self, _message: str) -> None:
            return None

    namespace = {
        "discord": SimpleNamespace(
            HTTPException=FakeHTTPException,
            Message=object,
            abc=SimpleNamespace(Messageable=object),
        ),
        "logger": FakeLogger(),
        "metrics": FakeMetrics(),
        "re": re,
    }
    exec(compile(module, str(SCRIPT_PATH), "exec"), namespace)
    return (
        namespace["DISCORD_MSG_LIMIT"],
        namespace["split_on_codeblocks"],
        namespace["send_discord_message"],
    )


DISCORD_MSG_LIMIT, split_on_codeblocks, send_discord_message = _load_helpers()


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.edits: list[str] = []

    async def edit(self, content: str) -> None:
        self.content = content
        self.edits.append(content)


class FakeChannel:
    def __init__(self) -> None:
        self.contents: list[str] = []
        self.messages: list[FakeMessage] = []

    async def send(self, content: str, **_kwargs) -> FakeMessage:
        message = FakeMessage(content)
        self.contents.append(content)
        self.messages.append(message)
        return message


def test_split_preserves_separator_after_codeblock() -> None:
    codeblock = "```py\nprint('x')\n```"
    content = f"{codeblock}\n\nParagraph2"

    parts = split_on_codeblocks(content, max_length=len(codeblock))

    assert parts == [codeblock, "\n\nParagraph2"]


def test_split_preserves_trailing_separator_before_codeblock() -> None:
    codeblock = "```py\nprint('x')\n```"
    content = f"Paragraph1\n\n{codeblock}"

    parts = split_on_codeblocks(content, max_length=len("Paragraph1\n\n"))

    assert parts == ["Paragraph1\n\n", codeblock]


def test_split_does_not_invent_separators_for_empty_text_blocks() -> None:
    content = "```py\nprint('x')\n```"

    parts = split_on_codeblocks(content, max_length=len(content) - 1)

    assert parts == [content]


@pytest.mark.asyncio
async def test_send_discord_message_splits_without_truncating() -> None:
    channel = FakeChannel()
    first = "A" * (DISCORD_MSG_LIMIT - 5)
    content = f"{first}\n\nSecond paragraph"

    message, had_error = await send_discord_message(channel, content)

    assert had_error is False
    assert channel.contents == [first, "\n\nSecond paragraph"]
    assert message is channel.messages[-1]


@pytest.mark.asyncio
async def test_send_discord_message_truncates_only_oversized_chunks() -> None:
    channel = FakeChannel()
    huge_codeblock = f"```{'x' * (DISCORD_MSG_LIMIT + 25)}```"
    content = f"Intro\n\n{huge_codeblock}\n\nOutro"

    message, had_error = await send_discord_message(channel, content)

    assert had_error is False
    assert channel.contents[0].startswith("```diff\n- Message too long")
    assert channel.contents[1] == "Intro\n\n"
    assert len(channel.contents[2]) == DISCORD_MSG_LIMIT
    assert channel.contents[2].endswith("...")
    assert channel.contents[3] == "\n\nOutro"
    assert message is channel.messages[-1]
