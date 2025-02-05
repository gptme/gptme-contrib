# Bob's Discord Bot

A Discord bot interface for Bob, powered by gptme. This bot allows Bob to interact with users on Discord, with full access to gptme's capabilities including running code, executing commands, and browsing the web.

## Setup

1. Create a Discord Application
   - Go to https://discord.com/developers/applications
   - Click "New Application"
   - Name it "Bob" (or whatever you prefer)
   - Go to the "Bot" section
   - Click "Add Bot"
   - Copy the bot token

2. Configure the Bot

   The bot uses two configuration files:
   - `.env`: Main project secrets (shared with gptme)
   - `.env.discord`: Discord-specific settings (overrides .env)

   Setup:
   ```bash
   # Copy example config
   cp .env.discord.example .env.discord

   # Edit config and add your token
   vim .env.discord
   ```

   Configuration Options:

   Required:
   - `DISCORD_TOKEN`: Your Discord bot token

   Optional:
   - `ENABLE_PRIVILEGED_INTENTS`: Set to "1" or "true" to enable privileged intents
   - `DEFAULT_MODEL`: Model to use (defaults to openai/gpt-4)
     - Format: `provider/model` (e.g., openai/gpt-4, anthropic/claude-3, local/llama2)
   - `RATE_LIMIT`: Minimum seconds between messages (defaults to 1.0)

   Environment File Priority:
   1. `.env.discord` (Discord-specific settings)
   2. `.env` (Shared gptme settings)

   The bot will:
   - Load both files if available
   - Use .env.discord values over .env
   - Log which files were loaded
   - Validate all settings on startup

   Note about Privileged Intents:
   - By default, the bot runs without privileged intents
   - If you need member or presence data:
     1. Go to Discord Developer Portal
     2. Select your application
     3. Go to "Bot" section
     4. Enable "Server Members Intent" and "Presence Intent"
     5. Set ENABLE_PRIVILEGED_INTENTS=true in .env.discord

3. Install Dependencies
   The bot uses uv's script metadata to manage dependencies. Simply running the script will install them:
   ```bash
   ./discord_bot.py
   ```

4. Using the Bot

   Direct Messages:
   - Right-click on the bot's name
   - Select "Message"
   - Start chatting!

   Adding to Server:
   - Use `!invite` command to get invite URL
   - Or use the URL shown in startup logs
   - Click the link and select your server
   - Bot needs these permissions:
     - Send Messages
     - Read Messages/View Channels
     - Add Reactions
     - Embed Links
     - Use External Emojis

   Testing:
   1. Start with DMs to test basic functionality
   2. Once working, invite to a test server
   3. Use `!checkperms` to verify permissions
   4. Try different commands: !about, !help, !status

5. Server Setup Best Practices
   - Use a dedicated channel for bot interactions
   - Ensure bot role has required permissions:
     - View Channel
     - Send Messages
     - Read Message History
     - Add Reactions
     - Embed Links
   - Bot will check permissions on startup and when joining servers
   - Use `!checkperms` to verify permissions at any time
   - Bot will notify about missing permissions when joining

6. Permission Management
   The bot includes several features for permission management:
   - Automatic permission checking on startup
   - Permission verification when joining new servers
   - Welcome message with permission status
   - `!checkperms` command for manual checks
   - Detailed logging of permission issues

## Usage

### User Commands
- `!help` - Show help message
- `!about` - Show information about Bob
- `!clear` - Clear conversation history
- `!status` - Show conversation status
- `!model [name]` - Get or set the model for this channel

### Admin Commands
- `!backup` - Backup all conversations
- `!shutdown` - Shutdown the bot gracefully

### Conversation Features
- Full access to gptme's capabilities
- Per-channel conversation history
- Rate limiting to prevent spam
- Automatic message splitting for long responses
- Code block formatting
- Error recovery
- Conversation persistence between restarts

### Channel Settings
Each channel maintains its own:
- Conversation history
- Model settings
- Configuration

## Directory Structure
## Directory Structure
## Directory Structure
```text
logs/discord/              # Root directory for Discord data
├── <channel_id>/         # Per-channel directory
│   ├── conversation.json # Current conversation state
│   ├── settings.txt     # Channel settings
│   └── backup/          # Conversation backups
│       └── conversation_<timestamp>.json
```

## Development

### Architecture
- Uses discord.py for Discord integration
- Integrates with gptme for AI capabilities
- Maintains separate conversations per channel
- Uses uv for dependency management
- Implements proper rate limiting and error handling

### Adding New Features
1. Commands
   - Add new command with `@bot.command()`
   - Update help text
   - Add to README

2. Channel Settings
   - Add to ChannelSettings class
   - Add load/save handling
   - Add command interface if needed

### Testing
1. Run with debug logging:
   ```bash
   GPTME_DEBUG=1 ./discord_bot.py
   ```

2. Test in development server first
3. Monitor logs for errors
4. Test rate limiting with multiple users
5. Verify conversation persistence

## Troubleshooting

### Common Issues

1. Bot Not Responding
   - Check if token is correct
   - Verify bot has proper permissions
   - Check logs for errors
   - Ensure rate limits aren't active

2. Message Send Failures
   - Check if content length exceeds limits
   - Verify bot has permission to send messages
   - Check for proper error handling

3. Conversation Issues
   - Try `!clear` to reset conversation
   - Check channel settings with `!status`
   - Verify model setting with `!model`

### Logs
- Check Discord bot logs in terminal
- Check gptme logs in logs/discord/<channel_id>/
- Error messages are formatted in code blocks
- System messages show in diff format

## Security Notes

1. Token Security
   - Never commit .env.discord
   - Rotate token if exposed
   - Use environment variables in production

2. Permissions
   - Minimize bot permissions
   - Use channel-specific settings
   - Admin commands restricted to owner

3. Rate Limiting
   - Prevents spam/abuse
   - Per-user limits
   - Channel-specific settings possible

## Development

### Type Checking

The bot uses mypy for static type checking. Configuration is in `mypy.ini`.

To run type checking:
```bash
# Using mypy directly
mypy discord_bot.py

# Using pre-commit
pre-commit run mypy --all-files
```

### Contributing

1. Fork the repository
2. Create feature branch
3. Ensure type hints are correct
4. Add tests if needed
5. Update documentation
6. Submit pull request

### Development Setup

1. Install dependencies:
   ```bash
   # Install pre-commit hooks
   pre-commit install

   # Install dev dependencies
   pip install mypy types-PyYAML types-requests discord.py
   ```

2. Configure environment:
   - Copy .env.discord.example to .env.discord
   - Set up Discord bot token
   - Configure any optional settings

3. Run type checks:
   ```bash
   mypy discord_bot.py
   ```

4. Test locally:
   ```bash
   ./discord_bot.py
   ```

## License

Same as gptme project
