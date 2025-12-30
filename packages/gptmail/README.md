# gptmail

Email automation for gptme agents with shared communication utilities.

## Overview

gptmail provides email automation capabilities including:
- CLI tools for reading, composing, and sending emails
- Background watcher for processing unreplied emails
- Shared communication utilities (auth, rate limiting, monitoring, state)
- Integration with Gmail via IMAP/SMTP

This package was originally developed as part of an agent workspace and upstreamed to gptme-contrib
and upstreamed to gptme-contrib for use by all gptme agents.

## Installation

From workspace root:
```bash
uv pip install -e packages/gptmail
```

Or using the Makefile:
```bash
cd packages/gptmail
make install
```

## Usage

### CLI Tools

```bash
# Check for unreplied emails
python -m gptmail check-unreplied

# Read specific email with thread
python -m gptmail read <MESSAGE_ID> --thread

# Compose a reply
python -m gptmail reply <MESSAGE_ID> "Your reply message"

# Send composed message
python -m gptmail send <REPLY_MESSAGE_ID>
```

### Background Watcher

```bash
# Run watcher in daemon mode
python -m gptmail.watcher

# Process one email and exit
python -m gptmail.watcher --mode one
```

## Configuration

Environment variables:
- `AGENT_EMAIL`: Default sender email address
- `EMAIL_ALLOWLIST`: Comma-separated list of allowed sender addresses
- `EMAIL_WORKSPACE`: Path to email workspace directory

## Architecture

The package structure:
- `src/gptmail/` - Main package code
  - `cli.py` - Command-line interface
  - `lib.py` - Core email library
  - `watcher.py` - Background email processor
  - `complexity.py` - Email complexity analysis
  - `communication_utils/` - Shared utilities (auth, rate limiting, etc.)
- `tests/` - Test suite
- `examples/` - Usage examples

## Migration from scripts/email

This package supersedes `gptme-contrib/scripts/email/`. Key differences:
- Proper Python package structure (src layout)
- Part of uv workspace
- Enhanced communication utilities
- Better test coverage

To migrate, replace:
```bash
# Old (scripts/email)
./gptme-contrib/scripts/email/cli.py check-unreplied

# New (gptmail package)
python -m gptmail check-unreplied
# Or if installed: gptmail check-unreplied
```

## Contributing

Contributions welcome! Please ensure:
- Tests pass: `make test`
- Type checking passes: `make typecheck`
- Code is formatted: `make format`
