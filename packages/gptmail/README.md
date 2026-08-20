# gptmail

Communication tooling for gptme agents, with two transports:
- **Email** (`gptmail …`) — Gmail via IMAP/SMTP, for talking to the outside world.
- **Inter-agent messaging** (`gptmail agent …`) — SSH/SCP between agent workspaces,
  email-free so it runs in isolated LXC sessions with no mail infra.

## Overview

gptmail provides:
- CLI tools for reading, composing, and sending emails
- Inter-agent SSH messaging (`gptmail agent`) — a filesystem inbox/outbox delivered
  over SSH, with no dependency on email infrastructure
- Background watcher for processing unreplied emails
- Shared communication utilities (auth, rate limiting, monitoring, state)
- Integration with Gmail via IMAP/SMTP

This package was originally developed as part of an agent workspace and upstreamed to gptme-contrib
for use by all gptme agents.

## Installation

### Standalone (recommended for agents without uv workspace)

```bash
# Using uv tool
uv tool install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail

# Or using pipx
pipx install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail
```

### From workspace

```bash
# From workspace root
uv pip install -e packages/gptmail

# Or using the Makefile
cd packages/gptmail && make install
```

## Usage

### CLI Tools

```bash
# Check for unreplied emails
gptmail check-unreplied

# Read specific email with thread
gptmail read <MESSAGE_ID> --thread

# Compose a reply
gptmail reply <MESSAGE_ID> "Your reply message"

# Send composed message
gptmail send <REPLY_MESSAGE_ID>

# See all commands
gptmail --help
```

Note: If installed in development mode, use `python -m gptmail` instead.

### Inter-agent messaging (`gptmail agent`)

Email-free messaging between agent workspaces, delivered over SSH/SCP. Each agent has
a `messages/` directory (`inbox/`, `outbox/`); sending writes to the local outbox and
SCPs the message into the recipient's remote inbox. No IMAP/SMTP — works in isolated
LXC sessions.

```bash
gptmail agent status                       # self name, registry, inbox/outbox counts
gptmail agent send bob "Subject" "Body"    # deliver to bob's inbox over SSH
gptmail agent list                         # unread inbox messages
gptmail agent read <MESSAGE_ID> --thread   # read (marks read), with thread
gptmail agent reply <MESSAGE_ID> "Body"    # threaded reply
gptmail agent pending                      # inbox messages awaiting a reply (SLA)
gptmail agent broadcast "Subject" "Body"   # send to every agent in the registry
```

- **Self name**: `AGENT_NAME` env var, else `$USER`. A human can use this to reach
  agents — set `AGENT_NAME=erik` to send as `erik` rather than your unix username.
- **Workspace**: resolved via `git rev-parse --show-toplevel`; the registry and your
  inbox/outbox live under `<workspace>/messages/`.
- **Registry** (`<workspace>/messages/agents.yaml`) maps names to SSH targets. Both
  `ssh` and `workspace` (remote workspace root) are required:

  ```yaml
  bob:
    ssh: bob@bob          # an ~/.ssh/config Host alias works
    workspace: bob        # remote path; message lands in <workspace>/messages/inbox/
  alice:
    ssh: alice@alice
    workspace: alice
  ```

- **Replies without inbound access**: an agent on an unreachable host (e.g. a laptop)
  can still receive replies by *pulling* — periodically `scp`/`rsync` the sender's
  remote `messages/outbox/` into your local inbox — instead of requiring the remote to
  SSH back.

Requires `pyyaml`. If installing standalone, add it:
`uv tool install --force --with pyyaml git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail`

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

## Credentials

**Best practice: use `pass` (the Unix password manager) for all email credentials.**

Storing passwords in plaintext files (`~/.email-password`, etc.) is convenient but insecure.
Agents running in server contexts should use `pass` instead:

```bash
# Install pass
sudo apt install pass

# Initialize a GPG key for the agent (no passphrase)
gpg --batch --gen-key <<EOF
Key-Type: RSA
Key-Length: 4096
Name-Real: Your Agent Name
Name-Email: agent@example.com
Expire-Date: 0
%no-protection
EOF

# Initialize the password store
pass init "agent@example.com"

# Store the email password (type it interactively when prompted)
pass insert email/agent-account
```

Then reference it in your mail config files:

**`~/.mbsyncrc`** (IMAP sync via isync):
```ini
IMAPAccount gmail
Host imap.gmail.com
User agent@gmail.com
PassCmd "pass email/agent-account"
SSLType IMAPS
```

**`~/.msmtprc`** (SMTP sending):
```ini
account gmail
host smtp.gmail.com
port 587
from agent@gmail.com
auth on
user agent@gmail.com
passwordeval "pass email/agent-account"
tls on
tls_starttls on
```

This ensures passwords are stored encrypted at rest and never appear in config files,
logs, or version control.

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

## Note: scripts/email Removed

The `gptme-contrib/scripts/email/` directory has been removed. This package (`gptmail`)
is now the canonical implementation. Key features:
- Proper Python package structure (src layout)
- Part of uv workspace
- Enhanced communication utilities
- Better test coverage
- Full maildir import/export support

Usage:
```bash
python -m gptmail check-unreplied
# Or if installed: gptmail check-unreplied
```

## Contributing

Contributions welcome! Please ensure:
- Tests pass: `make test`
- Type checking passes: `make typecheck`
- Code is formatted: `make format`
