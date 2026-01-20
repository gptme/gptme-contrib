# Email System

Universal email system for AI agents, supporting both internal agent communication and external email integration.

## Quick Start

From your agent workspace root:

```bash
# Set up email directories
mkdir -p email/{inbox,sent,archive,drafts,filters}

# Use the email system (run as Python module)
python -m gptmail compose recipient@example.com "Subject" "Message"
python -m gptmail list
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
    └── packages/
        └── gptmail/    # Email system package
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
python -m gptmail compose recipient@example.com "Project Update" "Status report..."
python -m gptmail send <message-id>

# Read with threading
python -m gptmail read <message-id> --thread

# Check for unreplied emails
python -m gptmail unreplied
```

This email system is part of the [gptme](https://github.com/gptme/gptme) ecosystem and designed to work with any AI agent workspace.
