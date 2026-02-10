#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "python-telegram-bot>=21.0",
#   "rich>=13.0.0",
#   "python-dotenv",
#   "gptme[telemetry] @ git+https://github.com/gptme/gptme.git",
# ]
# ///
# ruff: noqa: E402
# mypy: ignore-errors
"""
Telegram bot for gptme - allows interaction with gptme through Telegram.

This bot shares infrastructure patterns with the Discord bot, using
the communication_utils module for state tracking.

Note: mypy errors are ignored because python-telegram-bot types are not
available in the pre-commit environment.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict

# Add scripts directory to path for imports
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.append(str(SCRIPTS_DIR))

from dotenv import load_dotenv
from rich.logging import RichHandler
from gptme.config import get_project_config

os.environ["GPTME_CHECK"] = "false"

# Max chars in a Telegram message
TELEGRAM_MSG_LIMIT = 4096

# Type aliases
ChatID = int
UserID = str

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("telegram_bot")


class SimpleRateLimiter:
    """Simple per-user rate limiter for Telegram."""

    def __init__(self, rate_limit: float):
        """Initialize rate limiter.

        Args:
            rate_limit: Minimum seconds between messages per user
        """
        self.rate_limit = rate_limit
        self._last_message: Dict[UserID, float] = {}

    def check_rate_limit(self, user_id: UserID) -> bool:
        """Check if user is within rate limit.

        Args:
            user_id: User identifier

        Returns:
            True if request can proceed, False if rate limited
        """
        now = time.time()
        last_time = self._last_message.get(user_id, 0)
        if now - last_time < self.rate_limit:
            return False
        self._last_message[user_id] = now
        return True


def main() -> None:
    """Run the bot."""
    # Import telegram here to avoid issues with type checking in pre-commit
    try:
        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError:
        logger.error(
            "python-telegram-bot not installed. Run: pip install python-telegram-bot"
        )
        sys.exit(1)

    from gptme.chat import step
    from gptme.dirs import get_project_gptme_dir
    from gptme.init import init
    from gptme.logmanager import Log, LogManager
    from gptme.message import Message
    from gptme.prompts import get_prompt
    from gptme.telemetry import init_telemetry, shutdown_telemetry
    from gptme.tools import ToolSpec, get_tools, init_tools

    # Import shared utilities from communication_utils
    from communication_utils.state.tracking import (
        ConversationTracker,
        MessageState as MsgState,
    )

    # Get workspace folder
    workspace_root = get_project_gptme_dir()
    logsdir = (
        workspace_root / "logs_telegram" if workspace_root else Path("logs_telegram")
    )
    # Ensure logs directory exists
    logsdir.mkdir(parents=True, exist_ok=True)

    # Load environment variables
    env_files = [".env", ".env.telegram"]
    for env_file in env_files:
        if workspace_root and (workspace_root / env_file).exists():
            load_dotenv(workspace_root / env_file, override=True)
            logger.info(f"Loaded environment from {env_file}")

    MODEL = os.getenv("MODEL", "anthropic")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    RATE_LIMIT = float(os.getenv("RATE_LIMIT", "1.0"))
    TRUSTED_USERS = [
        u.strip() for u in os.getenv("TRUSTED_USERS", "").split(",") if u.strip()
    ]

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set. Please set it in .env or .env.telegram")
        sys.exit(1)

    # Log configuration
    logger.info("Configuration:")
    logger.info(f"  Rate limit: {RATE_LIMIT}s")
    logger.info(f"  Default model: {MODEL}")
    logger.info(f"  Logs directory: {logsdir}")
    logger.info(f"  Trusted users: {TRUSTED_USERS}")

    def get_bot_name() -> str:
        """Get agent name from gptme.toml config, fallback to environment or default."""
        try:
            # Try to get agent name from gptme.toml
            project_config = get_project_config(workspace_root)
            if project_config and project_config.agent and project_config.agent.name:
                return project_config.agent.name
        except Exception:
            pass

        # Fallback to environment variable
        return os.environ.get("AGENT_NAME", "Agent")

    bot_name = get_bot_name()

    # Basic tools only for safety
    tool_allowlist = ["read", "save", "append", "patch", "shell"]

    # Initialize gptme
    init(
        MODEL, interactive=False, tool_allowlist=tool_allowlist, tool_format="markdown"
    )
    init_tools(tool_allowlist)
    tools: list[ToolSpec] = get_tools()
    init_telemetry(
        service_name="gptme-telegram",
        agent_name="telegram-bot",
        interactive=False,  # Telegram bot runs non-interactively
    )

    logger.info(f"Initialized with {len(tools)} tools")

    # Initialize shared utilities
    state_dir = logsdir / "state"
    conversation_tracker = ConversationTracker(state_dir)
    rate_limiter = SimpleRateLimiter(RATE_LIMIT)

    # Global state
    conversations: dict[ChatID, Log] = {}

    def get_conversation(chat_id: ChatID) -> Log:
        """Get or create a conversation log for a chat."""
        if chat_id not in conversations:
            log_path = logsdir / f"chat_{chat_id}"
            log_path.mkdir(parents=True, exist_ok=True)
            initial_msgs = get_prompt(tools=tools, interactive=False)
            logmanager = LogManager.load(log_path, initial_msgs=initial_msgs)
            conversations[chat_id] = logmanager.log
            logger.info(f"Created new conversation for chat {chat_id}")
        return conversations[chat_id]

    def split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
        """Split a message into chunks that fit within Telegram's limit."""
        if len(text) <= limit:
            return [text]

        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break

            # Find a good split point (newline or space)
            split_point = text.rfind("\n", 0, limit)
            if split_point == -1:
                split_point = text.rfind(" ", 0, limit)
            if split_point == -1:
                split_point = limit

            chunks.append(text[:split_point])
            text = text[split_point:].lstrip()

        return chunks

    async def start_command(update: Update, context: object) -> None:
        """Handle /start command."""
        if update.message is None:
            return

        await update.message.reply_text(
            f"👋 Hello! I'm {bot_name}, a gptme-powered assistant.\n\n"
            "Send me a message and I'll help you with coding, questions, and more.\n\n"
            "Commands:\n"
            "/start - Show this message\n"
            "/clear - Clear conversation history\n"
            "/help - Show help"
        )

    async def clear_command(update: Update, context: object) -> None:
        """Handle /clear command."""
        if update.message is None:
            return

        chat_id = update.message.chat_id
        if chat_id in conversations:
            del conversations[chat_id]
            await update.message.reply_text("🗑️ Conversation cleared!")
        else:
            await update.message.reply_text("No conversation to clear.")

    async def help_command(update: Update, context: object) -> None:
        """Handle /help command."""
        if update.message is None:
            return

        await update.message.reply_text(
            f"🤖 {bot_name} Help\n\n"
            "I'm an AI assistant powered by gptme. I can help with:\n"
            "• Answering questions\n"
            "• Writing and reviewing code\n"
            "• Explaining concepts\n"
            "• General assistance\n\n"
            "Just send me a message to get started!"
        )

    async def handle_message(update: Update, context: object) -> None:
        """Handle incoming messages."""
        if update.message is None or update.message.text is None:
            return

        message = update.message
        user = message.from_user
        chat_id = message.chat_id

        if user is None:
            return

        username = user.username or str(user.id)
        user_id = str(user.id)
        logger.info(f"[message] @{username}: {message.text}")

        # Track message state using conversation tracker
        conversation_id = f"telegram_{chat_id}"
        message_id = str(message.message_id)
        conversation_tracker.track_message(
            conversation_id=conversation_id,
            message_id=message_id,
        )
        conversation_tracker.set_message_state(
            conversation_id=conversation_id,
            message_id=message_id,
            state=MsgState.IN_PROGRESS,
        )

        # Check if user is trusted
        is_trusted = username in TRUSTED_USERS or user_id in TRUSTED_USERS
        if TRUSTED_USERS and not is_trusted:
            await message.reply_text(
                "🔒 Sorry, I'm currently only responding to trusted users.\n"
                "This is a temporary security measure."
            )
            return

        # Rate limiting
        if not rate_limiter.check_rate_limit(user_id):
            await message.reply_text(
                "⏳ Please wait a moment before sending another message."
            )
            return

        # Get or create conversation
        log = get_conversation(chat_id)

        # Add user message to log
        log.append(Message(role="user", content=message.text))

        try:
            # Generate response using gptme
            # confirm=lambda _: True auto-confirms all tool uses (non-interactive)
            response_parts = []

            for msg in step(log, stream=False, confirm=lambda _: True, model=MODEL):
                if msg.role == "assistant" and msg.content:
                    response_parts.append(msg.content)
                    log.append(msg)

            # Combine response
            full_response = (
                "\n".join(response_parts)
                if response_parts
                else "I couldn't generate a response."
            )

            # Split and send response
            for chunk in split_message(full_response):
                await message.reply_text(chunk)

            # Update state
            conversation_tracker.set_message_state(
                conversation_id=conversation_id,
                message_id=message_id,
                state=MsgState.COMPLETED,
            )

        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            conversation_tracker.set_message_state(
                conversation_id=conversation_id,
                message_id=message_id,
                state=MsgState.FAILED,
                error=str(e),
            )
            await message.reply_text(
                "❌ Sorry, I encountered an error processing your message. "
                "Please try again."
            )

    async def error_handler(update: object, context: object) -> None:
        """Handle errors."""
        logger.error(f"Exception while handling an update: {context}")

    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Add error handler
    application.add_error_handler(error_handler)

    logger.info("Starting Telegram bot...")

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    # Cleanup
    shutdown_telemetry()


if __name__ == "__main__":
    main()