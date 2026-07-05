# Telegram Bot for gptme

A Telegram bot that provides access to gptme's AI assistant capabilities through Telegram.

## Features

- 💬 Natural conversation with gptme
- 🔧 Access to gptme tools (read, save, append, patch, shell)
- 📊 Per-user rate limiting
- 🔒 Trusted user allowlist for security
- 📝 Conversation history per chat
- 🔄 State tracking (shared with Discord bot via `ConversationTracker`)

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token provided

### 2. Configure Environment

Create a `.env.telegram` file in your gptme workspace:

```bash
TELEGRAM_TOKEN=your_bot_token_here
MODEL=anthropic  # or openai, etc.
RATE_LIMIT=1.0  # seconds between messages
TRUSTED_USERS=username1,username2  # comma-separated usernames or user IDs
AGENT_NAME=Agent  # bot display name
```

### 3. Run the Bot

```bash
# Direct execution (uses uv for dependencies)
./telegram_bot.py

# Or with uv explicitly
uv run telegram_bot.py
```

## Commands

- `/start` - Welcome message and introduction
- `/clear` - Clear conversation history
- `/help` - Show help information

## Architecture

This bot shares some infrastructure with the Discord bot through `communication_utils/`:

- **State Tracking**: `ConversationTracker` - Conversation state management (shared)
- **Rate Limiting**: Simple local implementation (not shared)

### Shared vs Bot-Specific Code

| Component | Location | Shared? |
|-----------|----------|---------|
| State tracking | `communication_utils/state/` | ✅ Yes |
| Rate limiting | `scripts/telegram/telegram_bot.py` | ❌ Simple local implementation |
| Bot logic | `scripts/telegram/` | ❌ Telegram-specific |
| Bot logic | `scripts/discord/` | ❌ Discord-specific |

**Note**: The rate limiter could be moved to `communication_utils/rate_limiting/` in a follow-up PR if desired. The current implementation uses a simple local rate limiter for simplicity.

## Security

By default, the bot only responds to users in the `TRUSTED_USERS` list. This is a security measure to prevent unauthorized access to gptme's capabilities.

To allow all users (not recommended for public bots):
- Leave `TRUSTED_USERS` empty or unset

## Logs

Conversation logs are stored in:
- `logs_telegram/chat_{chat_id}/` - Per-chat conversation logs
- `logs_telegram/state/` - State tracking data

## Comparison with Discord Bot

| Feature | Telegram | Discord |
|---------|----------|---------|
| Message limit | 4096 chars | 2000 chars |
| Rate limiting | ✅ Local implementation | ✅ Local implementation |
| State tracking | ✅ Shared `ConversationTracker` | ✅ Shared `ConversationTracker` |
| Trusted users | ✅ | ✅ |
| Commands | /start, /clear, /help | !help, !clear, etc. |

## Development

The bot uses the same patterns as the Discord bot:
- `python-telegram-bot` library for Telegram API
- gptme for AI capabilities
- Shared utilities from `communication_utils/`

To add new features, consider whether they should be:
1. Telegram-specific (add to this bot)
2. Shared (add to `communication_utils/`)
