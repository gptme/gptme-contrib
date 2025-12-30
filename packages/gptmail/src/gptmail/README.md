# Email System

Universal email system for AI agents, supporting both internal agent communication and external email integration.

## Quick Start

From your agent workspace root:

```bash
# Set up email directories
mkdir -p email/{inbox,sent,archive,drafts,filters}

# Use the email system
./gptme-contrib/scripts/email/cli.py compose recipient@example.com "Subject" "Message"
./gptme-contrib/scripts/email/cli.py list
```

## Features

- **Universal Message Format**: Git-friendly Markdown format with email headers
- **External Email Integration**: SMTP/IMAP support for real email providers
- **Mail Client Compatibility**: Works with mutt, notmuch, and other standard tools
- **Threading Support**: Automatic conversation threading
- **Auto-Response**: Intelligent watcher for automated email handling

## Directory Structure

The email system expects these directories in your agent workspace:

```text
your-agent-workspace/
├── email/
│   ├── inbox/          # Received messages
│   ├── sent/           # Sent messages
│   ├── archive/        # Archived messages
│   ├── drafts/         # Draft messages
│   └── filters/        # Email filtering rules
└── gptme-contrib/      # This repository (as submodule)
    └── scripts/
        └── email/      # Email system code
```

## Configuration

The system automatically detects the agent workspace by navigating up from the script location. For custom workspace locations, set environment variables (TODO: implement GPTME_WORKSPACE support).

## External Email Setup

For real email integration, configure:

1. **mbsync/isync** for IMAP synchronization
2. **msmtp** for SMTP sending
3. **Gmail labels** (configurable via MAILDIR_INBOX/MAILDIR_SENT) for filtering

See the full documentation in this directory for detailed setup instructions.

## Usage Examples

```bash
# Compose and send
./gptme-contrib/scripts/email/cli.py compose recipient@example.com "Project Update" "Status report..."
./gptme-contrib/scripts/email/cli.py send <message-id>

# Read with threading
./gptme-contrib/scripts/email/cli.py read <message-id> --thread

# Auto-response watcher
./gptme-contrib/scripts/email/watcher.py
```

This email system is part of the [gptme](https://github.com/gptme/gptme) ecosystem and designed to work with any AI agent workspace.
