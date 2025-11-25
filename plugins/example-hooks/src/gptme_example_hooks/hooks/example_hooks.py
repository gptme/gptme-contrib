"""Example hook implementations demonstrating gptme hook system usage."""

import logging
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.message import Message

if TYPE_CHECKING:
    from gptme.logmanager import Log, LogManager
    from gptme.tools.base import ToolUse

logger = logging.getLogger(__name__)


def session_start_hook(
    logdir: Path,
    workspace: Path | None,
    initial_msgs: list[Message],
) -> Generator[Message, None, None]:
    """Hook that runs at session start.

    Demonstrates:
    - SESSION_START hook type
    - Accessing logdir, workspace, initial messages
    - Yielding system messages

    Args:
        logdir: Directory where conversation log is stored
        workspace: Optional workspace directory path
        initial_msgs: List of initial messages in the conversation
    """
    logger.info("Example hooks plugin: Session starting")

    # Generate a welcome message
    workspace_info = f" in workspace {workspace}" if workspace else ""
    yield Message(
        "system",
        f"🎯 Example hooks plugin loaded{workspace_info}!\n\n"
        f"This plugin demonstrates the gptme hook system.",
    )


def tool_pre_execute_hook(
    log: "Log",
    workspace: Path | None,
    tool_use: "ToolUse",
) -> Generator[Message, None, None]:
    """Hook that runs before any tool executes.

    Demonstrates:
    - TOOL_PRE_EXECUTE hook type
    - Accessing tool information
    - Validating or logging tool usage

    Args:
        log: The conversation log
        workspace: Optional workspace directory path
        tool_use: The tool about to be executed
    """
    logger.info(f"Example hooks plugin: Tool about to execute - {tool_use.tool}")

    # Example: Log tool usage
    yield Message("system", f"🔧 About to execute tool: {tool_use.tool}")

    # Example: You could add validation here
    # if tool_use.tool == "shell" and "rm -rf" in tool_use.content:
    #     yield Message("system", "❌ Dangerous command blocked!")
    #     yield StopPropagation()  # Prevent tool execution


def message_post_process_hook(
    manager: "LogManager",
) -> Generator[Message, None, None]:
    """Hook that runs after processing a message.

    Demonstrates:
    - MESSAGE_POST_PROCESS hook type
    - Accessing conversation state via LogManager
    - Reacting to processed messages

    Args:
        manager: The conversation manager with log and workspace
    """
    if not manager.log.messages:
        return

    last_msg = manager.log.messages[-1]
    logger.info(
        f"Example hooks plugin: Processed {last_msg.role} message "
        f"({len(last_msg.content)} chars)"
    )

    # Example: Analytics or reactions
    # (In real plugin, you might send to analytics service)
    yield Message(
        "system",
        f"✅ Processed {last_msg.role} message ({len(last_msg.content)} chars)",
    )


def register() -> None:
    """Register all example hooks with gptme.

    This function is called automatically by gptme's plugin system
    when the plugin is loaded. It registers all hooks defined in this module.

    Hook registration includes:
    - name: Unique identifier (conventionally plugin_name.hook_name)
    - hook_type: When the hook should run (from HookType enum)
    - func: The hook function to call
    - priority: Execution order (higher runs first, default 0)
    """
    logger.info("Example hooks plugin: Registering hooks")

    # Register session start hook (priority 100 - runs early)
    register_hook(
        name="example_hooks.session_start",
        hook_type=HookType.SESSION_START,
        func=session_start_hook,
        priority=100,
    )

    # Register tool pre-execute hook (default priority 0)
    register_hook(
        name="example_hooks.tool_pre_execute",
        hook_type=HookType.TOOL_PRE_EXECUTE,
        func=tool_pre_execute_hook,
        priority=0,
    )

    # Register message post-process hook (priority -100 - runs late)
    register_hook(
        name="example_hooks.message_post_process",
        hook_type=HookType.MESSAGE_POST_PROCESS,
        func=message_post_process_hook,
        priority=-100,
    )

    logger.info("Example hooks plugin: Registration complete")
