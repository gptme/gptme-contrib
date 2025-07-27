#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "discord.py>=2.3.0",
#   "rich>=13.0.0",
#   "python-dotenv",
#   "gptme @ git+https://github.com/gptme/gptme.git",
# ]
# [tool.uv]
# exclude-newer = "2025-05-05T00:00Z"
# ///

import asyncio
import logging
import os
import re
from copy import copy
from pathlib import Path
from typing import (
    AsyncGenerator,
    Callable,
    Dict,
    Optional,
    TypeAlias,
    Union,
)

from dotenv import load_dotenv
from gptme.chat import Message, step
from gptme.dirs import get_project_gptme_dir
from gptme.init import init
from gptme.logmanager import Log, LogManager
from gptme.prompts import get_prompt
from gptme.tools import (
    ToolSpec,
    ToolUse,
    get_tools,
    init_tools,
)
from rich.logging import RichHandler

import discord
from discord.ext import commands

os.environ["GPTME_CHECK"] = "false"


# Type aliases
ChannelID: TypeAlias = int
CommandPrefix = Union[str, Callable[..., str]]  # Type for command prefix
Settings: TypeAlias = Dict[ChannelID, "ChannelSettings"]
Conversations: TypeAlias = Dict[ChannelID, Log]
RateLimits: TypeAlias = Dict[int, float]

# Global state with type hints
conversations: Conversations = {}  # channel_id -> conversation log
channel_settings: Settings = {}  # channel_id -> settings
rate_limits: RateLimits = {}  # user_id -> last_message_time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("discord_bot")

# Get workspace folder (location of `gptme.toml`):
workspace_root = get_project_gptme_dir()
logsdir = workspace_root / "logs_discord" if workspace_root else Path("logs_discord")

tools: list[ToolSpec] = []

# Load environment variables
env_files = [".env", ".env.discord"]
for env_file in env_files:
    if workspace_root and (workspace_root / env_file).exists():
        load_dotenv(env_file, override=True)
        logger.info(f"Loaded environment from {env_file}")

MODEL = os.getenv("MODEL", "anthropic")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RATE_LIMIT = float(os.getenv("RATE_LIMIT", "1.0"))  # seconds between messages
ENABLE_PRIVILEGED_INTENTS = os.getenv("ENABLE_PRIVILEGED_INTENTS", "").lower() in [
    "1",
    "true",
]


# Log configuration
logger.info("Configuration:")
logger.info(f"  Rate limit: {RATE_LIMIT}s")
logger.info(f"  Default model: {MODEL}")
logger.info(f"  Logs directory: {logsdir}")

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True  # Required for reading message content
bot_name = "Bob"  # TODO: load from auth

# Optional privileged intents, must be enabled in Discord Developer Portal
if ENABLE_PRIVILEGED_INTENTS:
    intents.members = True  # Privileged intent
    intents.presences = True  # Privileged intent
    logger.info("Using privileged intents (members, presences)")
else:
    logger.info("Running without privileged intents")

# Define command prefix
COMMAND_PREFIX: str = "!"


class BobBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


bot = BobBot(
    command_prefix=COMMAND_PREFIX,
    description="Bob - A gptme-powered Discord bot",
    intents=intents,
)


# Helper function for command prefix check
def is_command(content: str) -> bool:
    """Check if a message is a command."""
    return content.startswith(COMMAND_PREFIX)


async def check_permissions(bot_user: Optional[discord.ClientUser]) -> list[str]:
    """Check if bot has required permissions in all channels."""
    if not bot_user:
        return ["Bot user not initialized"]

    missing_permissions = []
    for guild in bot.guilds:
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)

            required_permissions = [
                ("send_messages", "Send Messages"),
                ("read_messages", "Read Messages"),
                ("add_reactions", "Add Reactions"),
                ("embed_links", "Embed Links"),
            ]

            for perm, name in required_permissions:
                if not getattr(permissions, perm):
                    missing_permissions.append(f"Missing '{name}' permission in #{channel.name} ({guild.name})")

    return missing_permissions


async def fetch_discord_history(channel: discord.abc.Messageable) -> str:
    """Fetch recent message history from Discord and format as context."""
    messages = []
    try:
        async for msg in channel.history(limit=100):
            author = bot_name if msg.author == bot.user else msg.author.name
            messages.append(f'<message from="{author}" time="{msg.created_at}">{msg.content}</message>')
    except discord.Forbidden:
        logger.warning("Cannot read message history")
        return ""
    except Exception as e:
        logger.warning(f"Error fetching history: {e}")
        return ""

    if not messages:
        return ""

    # Reverse to get chronological order
    messages.reverse()
    context = "Recent chat context:\n" + "\n".join(messages)
    return context


async def async_step(
    log: Log,
    channel_id: int,
    channel: discord.abc.Messageable,
) -> AsyncGenerator[Message, None]:
    """Async wrapper around gptme.chat.step that supports multiple tool executions."""

    def confirm_func(msg: str) -> bool:
        # Auto-confirm all tool executions in Discord
        return True

    # Get channel settings
    settings = get_settings(channel_id)

    # Basic tools only for testing
    tool_allowlist = ["read", "save", "append", "patch", "shell", "ipython", "browser"]

    # Restrict tools
    # TODO: do this in a non-global way
    global tools
    tools = init_tools(tool_allowlist)
    logger.info(f"Successfully initialized gptme with tools ({', '.join(tool_allowlist)}) for channel {channel_id}")

    async def add_discord_context(log: Log) -> Log:
        # Fetch Discord history and create temporary log with context
        discord_context = await fetch_discord_history(channel)
        log = copy(log)
        if discord_context:
            context_msg = Message(
                "system",
                f"<chat-history>\n{discord_context}\n</chat-history>",
                hide=True,
            )
            log.messages.insert(-1, context_msg)  # Insert before last message
        return log

    # Run step in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    current_log = log

    while True:
        try:
            # has ephemeral discord context message
            request_log = await add_discord_context(current_log)
            # logger.debug(f"Starting step with log: {request_log}")
            messages = await loop.run_in_executor(
                None,
                lambda: list(
                    step(
                        request_log,
                        stream=True,
                        confirm=confirm_func,
                        tool_format="markdown",
                        workspace=workspace_root,
                        model=settings.model,
                    )
                ),
            )
            for msg in messages:
                yield msg
                # Update current log with the new message
                current_log = current_log.append(msg)

            # Check if there are any runnable tools left in the last assistant message
            last_content = next(
                (m.content for m in reversed(current_log) if m.role == "assistant"),
                "",
            )
            has_runnable = any(tooluse.is_runnable for tooluse in ToolUse.iter_from_content(last_content))
            if not has_runnable:
                break

        except Exception as e:
            logger.exception("Error in gptme step execution")
            raise RuntimeError(f"Error processing message: {str(e)}")


def validate_config() -> tuple[bool, str]:
    """Validate configuration and return (is_valid, error_message)."""
    # Check for token
    token = DISCORD_TOKEN
    if not token:
        return False, "DISCORD_TOKEN not set in .env or .env.discord"
    if token == "your_token_here":
        return False, "DISCORD_TOKEN not properly configured"

    # Validate rate limit
    try:
        rate_limit = float(RATE_LIMIT)
        if rate_limit <= 0:
            return False, "RATE_LIMIT must be positive"
    except ValueError:
        return False, "RATE_LIMIT must be a valid number"

    # Log validation success with settings
    logger.info("Configuration validated:")
    logger.info(f"  Rate limit: {rate_limit}s")
    logger.info(f"  Privileged intents: {ENABLE_PRIVILEGED_INTENTS}")

    return True, ""


class ChannelSettings:
    model: str
    channel_id: int

    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.model = MODEL
        self.load()

    def load(self) -> None:
        """Load settings from disk."""
        settings_file = logsdir / str(self.channel_id) / "settings.txt"
        if settings_file.exists():
            settings = settings_file.read_text().splitlines()
            for line in settings:
                if line.startswith("model="):
                    self.model = line.split("=", 1)[1]

    def save(self) -> None:
        """Save settings to disk."""
        settings_file = logsdir / str(self.channel_id) / "settings.txt"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(f"model={self.model}\n")


def get_settings(channel_id: ChannelID) -> ChannelSettings:
    """Get or create settings for a channel."""
    if channel_id not in channel_settings:
        settings = ChannelSettings(channel_id)
        channel_settings[channel_id] = settings
    return channel_settings[channel_id]


def get_conversation(channel_id: ChannelID) -> Log:
    """Get or create a conversation for a channel."""
    initial_msgs = get_prompt(tools=tools)
    assert initial_msgs

    # Initialize a new conversation
    if channel_id not in conversations:
        logpath = logsdir / str(channel_id)
        logpath.mkdir(parents=True, exist_ok=True)
        logger.info(f"Loading conversation log for channel {channel_id} ({logpath})")
        # TODO: actually save conversations so there is something to load/resume, persisting tooluse responses across messages
        manager = LogManager.load(logpath, initial_msgs, create=True)
        conversations[channel_id] = manager.log

    # always keep system messages fresh
    # strip leading system messages, replace with initial_msgs
    msgs = copy(conversations[channel_id].messages)
    while msgs and msgs[0].role == "system" and "<chat-history>" not in msgs[0].content:
        msgs.pop(0)
    for msg in reversed(initial_msgs):
        msgs.insert(0, msg)

    assert msgs
    return Log(msgs)


def check_rate_limit(user_id: int, channel: discord.abc.Messageable) -> tuple[bool, float]:
    """Check if user is rate limited.

    Returns:
        tuple[bool, float]: (is_allowed, seconds_remaining)
    """
    now = asyncio.get_event_loop().time()

    # Use lower rate limit for DMs during testing
    effective_rate_limit = RATE_LIMIT * 0.5 if isinstance(channel, discord.DMChannel) else RATE_LIMIT

    if user_id in rate_limits:
        time_since_last = now - rate_limits[user_id]
        if time_since_last < effective_rate_limit:
            seconds_remaining = effective_rate_limit - time_since_last
            logger.debug(
                f"Rate limit hit: user={user_id} "
                f"channel_type={'DM' if isinstance(channel, discord.DMChannel) else 'server'} "
                f"seconds_remaining={seconds_remaining:.1f}"
            )
            return False, seconds_remaining

    rate_limits[user_id] = now
    return True, 0.0


async def handle_rate_limit(message: discord.Message) -> bool:
    """Handle rate limiting for a message.

    Returns:
        bool: True if message should be processed, False if rate limited
    """
    is_allowed, seconds_remaining = check_rate_limit(message.author.id, message.channel)

    if not is_allowed:
        # Add rate limit reaction
        await message.add_reaction("⏳")

        # Send rate limit message if significant time remaining
        if seconds_remaining > 1.0:
            await message.channel.send(
                f"```diff\n- Please wait {seconds_remaining:.1f}s before sending another message\n```",
                delete_after=min(seconds_remaining, 5.0),
            )
        return False

    return True


def split_on_codeblocks(content: str, max_length: int = 2000) -> list[str]:
    """Split content into parts, trying to keep code blocks intact."""
    if len(content) <= max_length:
        return [content]

    parts = []
    current = ""

    # Split on code blocks
    blocks = content.split("```")
    for i, block in enumerate(blocks):
        is_codeblock = i % 2 == 1

        # Add code block markers
        if is_codeblock:
            block = f"```{block}```"

        # If adding this block would exceed limit, start new part
        if len(current) + len(block) > max_length:
            if current:
                parts.append(current)
            current = block
        else:
            current += block

    if current:
        parts.append(current)

    return parts


async def send_discord_message(
    channel: discord.abc.Messageable,
    content: str,
    current_response: Optional[discord.Message] = None,
) -> tuple[Optional[discord.Message], bool]:
    """Send a message to Discord, handling length limits and logging.

    Args:
        channel: Channel to send message to
        content: Content to send
        current_response: Existing message to edit, if any

    Returns:
        Tuple of (message, had_error)
    """
    try:
        # Log full message content
        logger.info(f"Sending message ({len(content)} chars):\n{content}")

        # Handle messages that exceed Discord's limit
        if len(content) > 4000:
            logger.warning(f"Message too long ({len(content)} chars), truncating")
            await channel.send("```diff\n- Message too long, truncating to 4000 chars\n```")
            content = content[:3997] + "..."

        # Split long messages
        if len(content) > 2000:
            parts = split_on_codeblocks(content)
            for i, part in enumerate(parts):
                logger.info(f"Sending part {i + 1}/{len(parts)} ({len(part)} chars)")
                if current_response and i == 0:
                    await current_response.edit(content=part)
                else:
                    current_response = await channel.send(part)
        else:
            if current_response:
                await current_response.edit(content=content)
            else:
                current_response = await channel.send(content)

        return current_response, False

    except discord.HTTPException as e:
        logger.error(f"Failed to send message: {e}")
        await channel.send("```diff\n- Error: Message too complex. Try a shorter response.\n```")
        return current_response, True


# Add regex pattern at top of file with other imports
re_thinking = re.compile(r"<think(ing)?>.*?(\n</think(ing)?>|$)", flags=re.DOTALL)


async def process_message(
    msg: Message,
    channel: discord.abc.Messageable,
    log: Log,
    current_response: Optional[discord.Message] = None,
    accumulated_content: str = "",
) -> tuple[Optional[discord.Message], bool, Log, str]:
    """Process a message from the assistant or system.

    Args:
        msg: Message to process
        channel: Channel to send message to
        log: Conversation log
        current_response: Existing message to edit, if any
        accumulated_content: Content accumulated so far in this step

    Returns:
        Tuple of (current_response, had_error, updated_log, accumulated_content)
    """
    if msg.role == "assistant":
        # Clean thinking tags and normalize newlines
        cleaned_content = re_thinking.sub("", msg.content)
        cleaned_content = re.sub(r"\n\n+", "\n", cleaned_content)

        # Append to accumulated content if streaming
        if current_response:
            accumulated_content = cleaned_content
        else:
            accumulated_content += cleaned_content

        # Send/update message
        current_response, had_error = await send_discord_message(channel, accumulated_content, current_response)

        # Only append to log if this is the final message
        if not had_error and len(cleaned_content.strip()) > 0:
            log = log.append(Message("assistant", cleaned_content))

        return current_response, had_error, log, accumulated_content

    elif msg.role == "system":
        # Only show important system messages
        firstline = msg.content.split("\n", 1)[0].lower()
        if "pre-commit" not in firstline and any(word in firstline for word in ["error", "warning", "failed"]):
            content = f"System: {msg.content[:1000]}..."
            await channel.send(content)
            had_error = True
        else:
            had_error = False
        log = log.append(msg)
        return current_response, had_error, log, accumulated_content

    return current_response, False, log, accumulated_content


@bot.command()
async def model(ctx: commands.Context, new_model: Optional[str] = None) -> None:
    """Get or set the model for this channel."""
    settings = get_settings(ctx.channel.id)

    if new_model:
        # Update model
        settings.model = new_model
        settings.save()
        await ctx.send(f"Model updated to: {new_model}")
    else:
        # Show current model
        await ctx.send(f"Current model: {settings.model}")


@bot.event
async def on_ready() -> None:
    """Called when the bot is ready and connected to Discord."""
    if not bot.user:
        logger.error("Bot user not initialized")
        return

    logger.info(f"[on_ready] Logged in as {bot.user} (ID: {bot.user.id})")

    # Generate invite URL
    invite_url = discord.utils.oauth_url(
        str(bot.user.id),  # Convert to string to satisfy type checker
        permissions=discord.Permissions(
            send_messages=True,
            read_messages=True,
            add_reactions=True,
            embed_links=True,
        ),
    )
    logger.info(f"Invite URL: {invite_url}")

    # Check permissions
    if bot.user:  # Ensure user is available
        missing_permissions = await check_permissions(bot.user)
        if missing_permissions:
            logger.warning("Missing permissions detected:")
            for missing in missing_permissions:
                logger.warning(f"  - {missing}")
        else:
            logger.info("All required permissions available")

    logger.info("------")
    logger.info("To test in DM: Right click bot name -> Message")
    logger.info("To add to server: Use invite URL above")


@bot.command()
async def invite(ctx: commands.Context) -> None:
    """Get the bot's invite URL and DM instructions."""
    if not bot.user:
        await ctx.send("❌ Bot not fully initialized")
        return

    invite_url = discord.utils.oauth_url(
        str(bot.user.id),  # Convert to string to satisfy type checker
        permissions=discord.Permissions(
            send_messages=True,
            read_messages=True,
            add_reactions=True,
            embed_links=True,
        ),
    )

    instructions = (
        "To interact with me:\n\n"
        "1. Add me to a server:\n"
        f"   {invite_url}\n\n"
        "2. Then to DM me:\n"
        "   - Find me in the server member list\n"
        "   - Right-click my name\n"
        "   - Select 'Message'\n\n"
        "Or try commands like !help and !about right here!"
    )

    await ctx.send(instructions)


@bot.command()
async def dm(ctx: commands.Context) -> None:
    """Test DM communication."""
    try:
        await ctx.author.send("👋 Hi! You can now DM me directly!")
        if ctx.guild:  # If command was used in a server
            await ctx.send("✅ Check your DMs!")
    except discord.Forbidden:
        await ctx.send("❌ I couldn't DM you. Please check if you have DMs enabled for server members.")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """Called when the bot joins a new server."""
    logger.info(f"[on_guild_join] Joined new guild: {guild.name} (ID: {guild.id})")

    # Only log permission issues, don't send messages
    missing_permissions = []
    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        required_permissions = [
            ("send_messages", "Send Messages"),
            ("read_messages", "Read Messages"),
            ("add_reactions", "Add Reactions"),
            ("embed_links", "Embed Links"),
        ]

        for perm, name in required_permissions:
            if not getattr(permissions, perm):
                missing_permissions.append(f"Missing '{name}' permission in #{channel.name}")

    if missing_permissions:
        logger.warning("Missing permissions in new guild:")
        for missing in missing_permissions:
            logger.warning(f"  - {missing}")


@bot.command()
@commands.has_permissions(administrator=True)
async def checkperms(ctx: commands.Context) -> None:
    """Check bot permissions in all channels."""
    missing_permissions = await check_permissions(bot.user)
    if missing_permissions:
        await ctx.send("⚠️ Missing permissions:\n" + "\n".join(f"- {p}" for p in missing_permissions))
    else:
        await ctx.send("✅ All required permissions are properly set!")


@bot.command()
async def clear(ctx: commands.Context) -> None:
    """Clear the conversation history in this channel."""
    channel_id = ctx.channel.id
    if channel_id in conversations:
        del conversations[channel_id]
        await ctx.send("Conversation history cleared! Starting fresh.")
    else:
        await ctx.send("No conversation history to clear.")


@bot.command()
async def status(ctx: commands.Context) -> None:
    """Show status of the current conversation."""
    channel_id = ctx.channel.id
    if channel_id in conversations:
        log = conversations[channel_id]
        msg_count = len(log)
        user_msgs = sum(1 for m in log if m.role == "user")
        assistant_msgs = sum(1 for m in log if m.role == "assistant")

        # Get current tools
        global tools
        tools_str = ", ".join(t.name for t in tools) if tools else "No tools loaded"

        await ctx.send(
            f"Conversation status:\n"
            f"- Total messages: {msg_count}\n"
            f"- User messages: {user_msgs}\n"
            f"- Assistant messages: {assistant_msgs}\n"
            f"- Available tools: {tools_str}"
        )
    else:
        await ctx.send("No active conversation in this channel.")


@bot.command("tools")
async def tools_(ctx: commands.Context) -> None:
    """Show available tools and their descriptions."""

    tools = get_tools()

    if not tools:
        await ctx.send("❌ No tools currently loaded.")
        return

    tool_info = "Available tools:\n\n"
    for tool in tools:
        tool_info += f"**{tool.name}**\n"
        tool_info += f"- Description: {tool.desc}\n"
        if tool.block_types:
            tool_info += f"- Usage: ```{', '.join(tool.block_types)}```\n"
        tool_info += "\n"

    # Split long messages if needed
    if len(tool_info) > 2000:
        parts = tool_info.split("\n\n")
        current = ""
        for part in parts:
            if len(current) + len(part) + 2 > 2000:
                await ctx.send(current)
                current = part + "\n\n"
            else:
                current += part + "\n\n"
        if current:
            await ctx.send(current)
    else:
        await ctx.send(tool_info)


@bot.command()
async def about(ctx: commands.Context) -> None:
    """Show information about Bob."""
    is_dm = isinstance(ctx.channel, discord.DMChannel)

    base_msg = (
        "👋 I'm Bob, an AI assistant powered by gptme!\n\n"
        "I can help with:\n"
        "- Programming and development\n"
        "- Running code and commands\n"
        "- Answering questions\n"
        "- And more!\n\n"
    )

    if is_dm:
        await ctx.send(
            f"{base_msg}"
            "🔑 Key Commands:\n"
            "- !help - Show available commands\n"
            "- !clear - Reset conversation\n"
            "- !status - Show conversation info\n"
            "- !model - View/change AI model\n\n"
            "⏱️ Note: Messages are rate-limited to prevent spam\n"
            "🔒 DMs are preferred for testing and development"
        )
    else:
        await ctx.send(
            f"{base_msg}"
            "📝 Note: For testing and development, please use DMs:\n"
            "1. Right-click my name\n"
            "2. Select 'Message'\n"
            "3. Start a private chat\n\n"
            "Or use !dm to start a DM conversation"
        )


async def update_reaction(
    message: discord.Message,
    remove_emoji: Optional[str] = None,
    add_emoji: Optional[str] = None,
) -> None:
    """Update reaction on a message."""
    if not bot.user:
        return

    try:
        if remove_emoji:
            await message.remove_reaction(remove_emoji, bot.user)
        if add_emoji:
            await message.add_reaction(add_emoji)
    except discord.HTTPException as reaction_error:
        logger.warning("Failed to update reaction: %s", reaction_error)


async def handle_gptme_error(message: discord.Message, error: Exception) -> None:
    """Handle errors from gptme processing."""
    logger.exception("Error in gptme processing", exc_info=error)
    error_msg = str(error)
    await update_reaction(message, "🤔", "❌")
    await message.channel.send(
        f"```diff\n- Error: Something went wrong.\n- Details: {error_msg}\n- Try !clear to reset the conversation.\n```"
    )


async def handle_new_dm(message: discord.Message) -> None:
    """Send welcome message for new DM conversations."""
    welcome_msg = (
        "👋 Welcome! You're now in a direct chat with Bob.\n\n"
        "🔒 DMs are the preferred way to interact during testing, as they provide:\n"
        "- More reliable tool access\n"
        "- Better error handling\n"
        "- Cleaner conversation history\n\n"
        "⚡ Quick Start:\n"
        "1. Try !about to learn what I can do\n"
        "2. Use !model to see/change the AI model\n"
        "3. Just start chatting!\n\n"
        "⏱️ Note: Messages are rate-limited to prevent spam"
    )
    await message.channel.send(welcome_msg)


async def process_conversation_step(
    message: discord.Message,
    channel_id: int,
    current_response: Optional[discord.Message] = None,
) -> tuple[Optional[discord.Message], bool]:
    """Process a single conversation step."""
    had_error = False
    accumulated_content = ""

    async for msg in async_step(conversations[channel_id], channel_id, message.channel):
        logger.info(f"Processing response msg: {msg}")
        (
            current_response,
            msg_error,
            updated_log,
            accumulated_content,
        ) = await process_message(
            msg,
            message.channel,
            conversations[channel_id],
            current_response,
            accumulated_content,
        )
        conversations[channel_id] = updated_log
        had_error = had_error or msg_error

    return current_response, had_error


@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming messages.

    TODO: Instead of restricting to trusted users, we should:
    1. Implement proper tool restrictions based on user trust level
    2. Add sandboxing for untrusted users
    3. Add rate limiting per trust level
    4. Add command restrictions per trust level
    """
    if message.author == bot.user:
        return

    logger.info(f"[on_message] @{message.author.name}: {message.content}")

    # Early returns for commands and rate limits
    if is_command(message.content):
        await bot.process_commands(message)
        return

    # Check if message is a DM or mentions the bot
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions if bot.user else False

    # Only process if DM or mentioned (unless it's a command)
    if not (is_dm or is_mentioned):
        return

    # Only allow trusted users (temporary security measure)
    is_trusted = message.author.name.lower() in ["erikbjare"]
    if not is_trusted:
        if is_mentioned:
            await message.channel.send(
                "🔒 Sorry, I'm currently only responding to trusted users for security reasons.\n"
                "This is a temporary measure until proper security features are implemented."
            )
        return

    if not await handle_rate_limit(message):
        return

    # Remove bot mention from message content if present
    content = message.content
    if is_mentioned and bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    # Initialize conversation
    channel_id = message.channel.id
    log = get_conversation(channel_id)
    logger.info(f"Lenght of log: {len(log)}")

    # Check if this is a new DM conversation by looking at message history
    if isinstance(message.channel, discord.DMChannel):
        history = await fetch_discord_history(message.channel)
        # If no previous messages from bot in history, this is a new conversation
        is_new_dm = not any(f'from="{bot_name}"' in msg for msg in history.split("\n"))
        if is_new_dm:
            await handle_new_dm(message)
            return  # Don't process the message further, let the welcome message be the only response

    # Add user message to conversation (only if we're not showing the welcome message)
    conversations[channel_id] = log.append(Message("user", content))

    try:
        # Setup message processing
        await message.add_reaction("⌛")
        await update_reaction(message, "⌛", "🤔")

        # Process message
        current_response = None
        async with message.channel.typing():
            current_response, had_error = await process_conversation_step(message, channel_id, current_response)

        # Update reaction based on result
        if had_error:
            await update_reaction(message, "🤔", "❌")
        else:
            await update_reaction(message, "🤔", None)

    except Exception as e:
        if isinstance(e, discord.HTTPException):
            logger.exception("Discord API error")
            await message.channel.send(
                "```diff\n- Error: Failed to send message. This might be due to message length or formatting.\n```"
            )
        else:
            await handle_gptme_error(message, e)


def main() -> None:
    # Validate configuration
    is_valid, error = validate_config()
    if not is_valid:
        logger.error(f"Configuration error: {error}")
        return

    # Load token
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN not set in .env.discord")
        return

    # Initialize gptme
    try:
        # Basic tools only for testing
        tool_allowlist = frozenset(["read", "save", "append", "patch", "shell"])

        # Initialize gptme and tools
        init(model=MODEL, interactive=False, tool_allowlist=list(tool_allowlist))
        tools = get_tools()
        if not tools:
            logger.error("No tools loaded in gptme")
            return
        logger.info(f"Loaded {len(tools)} gptme tools: {', '.join(t.name for t in tools)}")
    except Exception as e:
        logger.error(f"Failed to initialize gptme tools: {e}")
        return

    try:
        # Run the bot
        bot.run(token)
    except discord.LoginFailure:
        logger.error("Failed to login. Check your token in .env.discord")
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Bot requires privileged intents. Either:\n"
            "1. Enable privileged intents in Discord Developer Portal, or\n"
            "2. Set ENABLE_PRIVILEGED_INTENTS=false in .env.discord"
        )
    except Exception:
        logger.exception("Error running bot")


if __name__ == "__main__":
    main()
