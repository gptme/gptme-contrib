# gptmail

**Email and agent-to-agent messaging for autonomous AI agents.** gptmail gives a
[gptme](https://github.com/gptme/gptme) agent (or any agent that can run shell
commands) a mailbox it can read, reply to, and keep track of: real email over your
existing mail setup (mbsync + msmtp), plus email-free messaging between agent
workspaces over SSH. Every message is a plain Markdown file in the agent's git
workspace, so the agent can read it with ordinary file tools and you can review it in
a diff.

**Status:** beta (`Development Status :: 4 - Beta`). The email commands and
`gptmail agent` messaging are used daily by long-running gptme agents. The
background watcher (`gptmail.watcher`) is experimental — see
[Running unattended](#running-unattended).

## Contents

- [Why gptmail](#why-gptmail)
- [Install](#install)
- [Quickstart: email](#quickstart-email)
- [Quickstart: agent-to-agent messaging](#quickstart-agent-to-agent-messaging)
- [How it works](#how-it-works)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Running unattended](#running-unattended)
- [Credentials](#credentials)
- [Python API](#python-api)
- [Development](#development)

## Why gptmail

An agent that runs for weeks needs to talk to people and other agents, and it has to
answer every message exactly once, even across restarts and parallel sessions.
gptmail handles that:

- **Messages as files.** Inbox, drafts, sent and archive are directories of Markdown
  files in the agent's workspace. Any tool that can read files can work with them,
  including an LLM.
- **Reply tracking.** gptmail records which messages have been answered (or
  deliberately skipped), so `check-unreplied` / `agent pending` answer the question
  "what do I still owe a reply to?".
- **Safe by default.** Outbound mail goes only to allowlisted recipients. Auto-replies
  go only to allowlisted senders. Cc'd mail and automated notifications are never
  auto-replied.
- **Two transports, one model.** `gptmail …` handles external email.
  `gptmail agent …` sends messages between agent workspaces over SSH/SCP. It needs no
  mail server and never imports the email stack, so it works in isolated sandboxes.

**Bring your own.** gptmail is one well-integrated option, not a requirement. gptme
agents work fine with any email client, chat bridge or message bus. Related
alternatives in this repo:

- [gptme-forum](../gptme-forum/README.md): a git-native forum with threads and
  @mentions.
- [gptme-coordination](../gptme-coordination/README.md): SQLite work claims and
  messaging.
- [gptme-whatsapp](../gptme-whatsapp/README.md), plus the
  [Discord](../../scripts/discord/README.md),
  [Telegram](../../scripts/telegram/README.md) and
  [Twitter](../../scripts/twitter/README.md) scripts. Several of these reuse gptmail's
  shared [`communication_utils`](src/gptmail/communication_utils/README.md).

**Pairs with [gptodo](../gptodo/README.md).** gptodo is the task tracker from the same
agent stack. A typical agent uses gptodo for "what should I work on" and gptmail for
"who am I talking to and what do I owe them".

## Install

The package name is `gptmail`. It installs one console script, `gptmail`, and
requires Python 3.10 or newer.

```bash
# As a standalone CLI tool, from the git repo subdirectory
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail"
# or
pipx install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail"

# Inside a gptme-contrib checkout (gptmail is a uv workspace member)
uv sync

# From an agent workspace that vendors gptme-contrib as a git submodule
uv pip install -e gptme-contrib/packages/gptmail
```

Optional extra: `gptmail[oauth]` adds Flask/Werkzeug/python-dotenv for the OAuth
callback server in `communication_utils.auth`. The Twitter script uses it; plain email
does not.

**External tools.** Email delivery shells out to `msmtp`. Fetching mail is done by
`mbsync` (isync), which writes a local Maildir that `gptmail sync-maildir` imports.
`gptmail agent` needs `git`, `ssh` and `scp`.

> **Note:** run `gptmail` from inside the agent's git workspace. It finds the
> workspace with `git rev-parse --show-toplevel`, keeps all mail under it, and loads
> `<workspace>/.env` at startup. Variables already set in the environment take
> precedence over `.env`.

## Quickstart: email

```bash
cd ~/my-agent                                    # the agent's git workspace
mkdir -p email/{inbox,sent,archive,drafts,filters}

cat >> .env <<'EOF'
AGENT_EMAIL=agent@example.com
AGENT_EMAIL_NAME=My Agent
EMAIL_SEND_ALLOWLIST=you@example.com             # who the agent may send to
EMAIL_ALLOWLIST=you@example.com                  # whose mail it should answer
EOF

# Write and send a message (send goes through msmtp)
gptmail compose you@example.com "Hello" "First message from your agent."
#   -> Created draft: <uuid>
gptmail send "<uuid>"

# Fetch and import incoming mail, then see what needs an answer
mbsync -a
gptmail sync-maildir                             # imports inbox + sent
gptmail check-unreplied                          # exits 1 if anything is unreplied

# Answer a message (threads correctly and quotes the original), then send it
gptmail reply "<message-id>" "Thanks, on it."
gptmail send "<reply-draft-id>"

# Or mark a message as handled without replying
gptmail mark-no-reply "<message-id>" --reason "informational"
```

The five folders under `email/` must exist; gptmail refuses to start without them.
Leave `EMAIL_SEND_ALLOWLIST` unset and `send` only allows the agent's own address and
built-in `example.com` placeholder addresses. Everything else is refused.

## Quickstart: agent-to-agent messaging

Each agent workspace gets a `messages/` directory with an `inbox/` and an `outbox/`.
`send` writes the message to your outbox and copies it into the recipient's remote
inbox with `scp`.

1. Create a registry in each workspace at `messages/agents.yaml`:

   ```yaml
   alice:
     ssh: alice@alice-host        # any ssh target; ~/.ssh/config aliases work
     workspace: /srv/alice        # remote workspace root; mail lands in <workspace>/messages/inbox/
   bob:
     ssh: bob@bob-host
     workspace: /srv/bob
   dana:
     delivery: pull-only          # e.g. a human on a laptop with no inbound SSH
   ```

2. Send, read and reply:

   ```bash
   export AGENT_NAME=alice                       # defaults to $USER
   gptmail agent status                          # self, registry, inbox/outbox/pending counts
   gptmail agent send bob "Deploy done" "v1.2 is live."
   echo "long body" | gptmail agent send bob "Report"   # body from stdin
   gptmail agent list                            # unread inbox messages (* = unread)
   gptmail agent read <file.md> --thread         # marks it read
   gptmail agent reply <file.md> "Thanks!"       # threaded via in_reply_to, marks original replied
   gptmail agent pending                         # what you still owe a reply to
   ```

If delivery fails, `send` exits non-zero and the outbox copy is stamped
`delivered: false`.

**Pull-only recipients.** A recipient that cannot accept inbound SSH (for example a
person on a laptop) is marked `delivery: pull-only`. Messages to it stay in the
sender's outbox, and the recipient fetches them:

```bash
gptmail agent pull --as dana                   # fetch messages addressed to dana from every SSH-reachable agent
gptmail agent pending --as dana                # which of those still need a reply
gptmail agent watch --once --pull              # block until a new message arrives, then pull it
```

`pull`, `watch` and `pending --fleet` run
`uv run gptmail agent pending --for <you> --json` on each remote agent over SSH. Each
remote workspace therefore needs gptmail available through `uv run`.

## How it works

### Email workspace layout

```text
<workspace>/
├── .env                      # optional; loaded at startup without overriding the environment
└── email/
    ├── inbox/  sent/  archive/  drafts/  filters/   # required (filters/ is reserved)
    ├── locks/                # reply-tracking state (email.json) + per-message processing locks
    ├── processed_state.txt
    └── .sync_state_<folder>.json   # incremental maildir import bookkeeping
```

Each message is one file named after its Message-ID (`<id>` with `@` replaced by
`_at_`, plus `.md`). The file holds RFC 822-style headers, a blank line, then a
Markdown body. On `send`, gptmail renders the body to `multipart/alternative`
(plain text + HTML) and pipes it to `msmtp`. A body that is already HTML is sent
as-is. The file moves from `drafts/` to `sent/` only after delivery succeeds.

**Receiving.** gptmail does not speak IMAP itself. `mbsync` syncs your provider into
a local Maildir (default `~/.local/share/mail/gmail/INBOX` and `.../Sent`), and
`gptmail sync-maildir` imports new messages into `email/inbox` and `email/sent`.
Duplicates are skipped. `export-maildir` / `import-maildir` convert between the
Markdown folders and Maildir, for example to read the agent's mail in mutt or neomutt.

**What counts as "unreplied".** A message in `inbox/` (add `-f archive` to also scan
the archive) is unreplied when all of these hold:

- it was addressed to the agent via To, Bcc or envelope delivery (Cc is never
  auto-replied);
- the sender matches `EMAIL_ALLOWLIST` and is not the agent itself;
- it does not look like an automated notification (login alerts, verification codes,
  no-reply senders, GitHub notifications and similar);
- it is not already marked replied or no-reply-needed;
- no message in `sent/` has an `In-Reply-To` pointing at it.

Sending a reply marks the original replied automatically.

**Outbound secret gate.** Before sending, `send` passes the draft to an optional
`redact` package if one is importable in the environment. A critical finding blocks
the send and is logged to `state/outbound-redact-blocks.jsonl`. If `redact` is not
installed, gptmail logs a warning and sends normally.

### Agent messaging layout

```text
<workspace>/messages/
├── agents.yaml               # registry: name -> {ssh, workspace} or {delivery: pull-only}
├── inbox/   outbox/          # default mailbox
├── mailboxes/<name>/{inbox,outbox}/   # optional named mailboxes (--mailbox <name>)
└── .tracking/                # shared reply-tracking state (channel "agent")
```

Messages are named `YYYYMMDD-HHMMSS-ffffff-<sender>-<subject>.md`, and that filename
is the message ID. Each file has YAML frontmatter: `from`, `to`, `timestamp`,
`subject`, `read`, `mailbox`, and optionally `in_reply_to`, `reply_expected: false`
(set by `--no-reply`), `replied: true` and `delivered: false`. `pending` scans the
files directly, so it gives the right answer on any host without shared state.
Inbox messages older than the reply window (7 days by default) stop counting as
pending. `--include-stale` shows them again.

Both transports implement the `gptmail.transport.Transport` protocol and share one
`ConversationTracker`. Each tracked message records its channel (`"email"` or
`"agent"`).

## Command reference

Run `gptmail --help` or `gptmail <command> --help` for full option details.

### Email (`gptmail <command>`)

| Command | What it does |
|---|---|
| `compose TO SUBJECT [CONTENT] [--from ADDR]` | Create a draft. No CONTENT opens `$EDITOR`; `-` reads the body from stdin (use this for large bodies). |
| `send MESSAGE_ID` | Deliver a draft via msmtp (recipient must pass `EMAIL_SEND_ALLOWLIST`) and move it to `sent/`. |
| `reply MESSAGE_ID [CONTENT] [--from ADDR]` | Create a threaded reply draft (`In-Reply-To`/`References`, quoted original). Accepts `\n` escapes; no CONTENT opens `$EDITOR`. |
| `list [FOLDER]` | List messages in a folder (default `inbox`). |
| `read MESSAGE_ID [--thread \| --thread-only]` | Show a message, the whole thread, or just its structure. |
| `thread MESSAGE_ID [--structure] [--stats]` | Show the conversation thread, its outline, or statistics. |
| `archive MESSAGE_ID` | Move a message to `archive/`. |
| `check-unreplied [-f FOLDER ...]` | List unreplied mail from allowlisted senders; exits 1 if any are found. |
| `mark-no-reply MESSAGE_ID [--reason TEXT]` | Mark a message handled without replying. |
| `list-completed [--status all\|replied\|no_reply_needed]` | Show reply-tracking history. |
| `check-completion-status MESSAGE_ID` | Debug the tracker entry for one message. |
| `process-unreplied [--dry-run] [-f FOLDER ...]` | Run `gptme --non-interactive` once per unreplied email so the agent answers it (see below). |
| `check-complexity [--threshold N] [--mark-complex]` | Score unreplied mail for length, questions, sensitive keywords and decision requests; optionally mark complex mail as no-reply-needed so automatic replies skip it. |
| `sync-maildir [FOLDER]` | Import from the external Maildir (default `all` = inbox + sent). |
| `export-maildir FOLDER DEST` / `import-maildir SOURCE FOLDER` | Convert between Markdown folders and a Maildir (`FOLDER` may be `all`). |

### Agent messaging (`gptmail agent <command>`)

| Command | What it does |
|---|---|
| `send TO SUBJECT [CONTENT] [--no-reply] [--mailbox NAME]` | Send to one agent (body from stdin if CONTENT is omitted). `--no-reply` marks it informational. |
| `broadcast SUBJECT [CONTENT] [--no-reply] [--mailbox NAME]` | Send to every agent in the registry except yourself. |
| `list [FOLDER] [-a/--all] [--mailbox NAME \| --all-mailboxes]` | List messages (inbox shows unread only unless `--all`). |
| `read MESSAGE_ID [--thread] [--mailbox NAME]` | Print a message and mark it read. |
| `reply MESSAGE_ID [CONTENT] [--mailbox NAME]` | Reply to the sender. Refuses if the message is already marked replied. |
| `pending [--as ID] [--include-stale] [--mailbox NAME \| --all-mailboxes]` | Inbox messages awaiting your reply. |
| `pending --for NAME [--fleet] [--json]` | Messages addressed to NAME in your outbox (with `--fleet`, in every registered agent's outbox). |
| `pull [--as ID] [--json] [--dry-run] [--notify-cmd CMD]` | Fetch messages addressed to you from every SSH-reachable agent's outbox. `--notify-cmd` runs CMD with `NEW_COUNT` and `SUMMARY` set. |
| `watch [--to NAME] [--from A,B] [--interval SECS] [--once] [--pull]` | Poll agents' outboxes and print one line per new message. Works with systemd/launchd or a terminal. |
| `status [--mailbox NAME \| --all-mailboxes]` | Identity, registry and inbox/outbox/pending counts. |

`scripts/agent-msg.py` at the repo root is a compatibility shim for the older
`agent-msg.py` command surface. It maps `list --needs-reply` to `pending` and passes
every other command through to `gptmail agent`.

## Configuration

All configuration is through environment variables or `<workspace>/.env`.

| Variable | Used by | Meaning |
|---|---|---|
| `AGENT_EMAIL` | email (**required**) | The agent's address: default sender and "self" for reply filtering. |
| `AGENT_EMAIL_NAME` | email | Display name for the `From` header. |
| `AGENT_EMAIL_ALIASES` | email | Comma-separated extra addresses that also count as the agent. |
| `EMAIL_SEND_ALLOWLIST` | `send` | Comma-separated recipients or bare domains allowed for outbound mail; `*` allows all. `+tag` suffixes are ignored when matching. Fails closed. |
| `EMAIL_ALLOWLIST` | unreplied detection | Comma-separated senders or domains whose mail should be answered; `*` allows all senders except the agent itself. |
| `MAILDIR_INBOX`, `MAILDIR_SENT` | `sync-maildir` | External Maildir paths (defaults under `~/.local/share/mail/gmail/`). |
| `MAILDIR_<NAME>` | `sync-maildir <name>` | Map additional folders, for example `MAILDIR_ARCHIVE`. |
| `EDITOR` | `compose`, `reply` | Editor used when no content is given (default `vim`). |
| `GPTME_WORKSPACE` | watcher | Workspace root for the background watcher. |
| `AGENT_NAME` | `agent` | This agent's name (default `$USER`, lowercased). |
| `AGENT_MSG_REPLY_WINDOW_DAYS` | `agent pending` | Reply window in days (default `7`; `0` disables the age cutoff). |
| `AGENT_MSG_NO_REPLY_SUBJECT_PATTERNS` | `agent pending` | Newline-separated, case-insensitive regexes. Matching subjects never count as pending. |

`send` passes `-a gmail` to msmtp when the sender address contains `gmail.com`, and
uses msmtp's default account otherwise.

## Running unattended

The supported path is the CLI on a schedule (cron, a systemd timer, or the agent's
own run loop):

```bash
mbsync -a && gptmail sync-maildir && gptmail process-unreplied
```

`process-unreplied` takes a per-message lock, so overlapping runs never handle the
same email twice. For each unreplied email it runs
`gptme --no-confirm --non-interactive`, with a prompt telling the agent to read the
email file and then either `gptmail reply` + `gptmail send` or
`gptmail mark-no-reply`. That prompt calls `uv run python3 -m gptmail …`, so the
workspace must be able to run gptmail through `uv`. Use `--dry-run` to preview.

**Experimental watcher.** `python -m gptmail.watcher` runs the same cycle every 30
seconds. `once` runs a single cycle and `one` handles a single email. It currently
assumes source-checkout conventions:

- it calls `mbsync gmail-INBOX` and `mbsync gmail-Sent`, so your mbsync channels must
  have those names;
- it invokes `cli.py` from the package source directory;
- its gptme prompt uses `./cli.py`.

Set `GPTME_WORKSPACE` so it finds the right workspace. Prefer the scheduled CLI
command above unless your setup matches these assumptions.

## Credentials

Keep mail passwords out of plaintext files and out of the repo. On a server, the Unix
password manager [`pass`](https://www.passwordstore.org/) works well. Store the
password once:

```bash
pass init "agent@example.com"          # needs a GPG key for that identity
pass insert email/agent-account
```

Then point mbsync and msmtp at it:

```ini
# ~/.mbsyncrc
IMAPAccount gmail
Host imap.gmail.com
User agent@gmail.com
PassCmd "pass email/agent-account"
SSLType IMAPS

# ~/.msmtprc
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

## Python API

```python
from gptmail.lib import AgentEmail

mail = AgentEmail("/path/to/workspace", "agent@example.com")  # or rely on AGENT_EMAIL
draft_id = mail.compose("you@example.com", "Subject", "Markdown body")
mail.send(draft_id)

for item in mail.get_unreplied_emails():          # oldest first
    print(item.sender, item.subject, item.message_id)
```

Transports for code that should work over either channel:

```python
from gptmail.transport.email import EmailTransport  # wraps AgentEmail
from gptmail.transport.agent import AgentTransport  # filesystem inbox/outbox, no email imports
```

`AgentTransport` writes only local files. Delivery is a `deliver(path, recipient) -> bool`
callback you inject; the CLI's callback uses SSH/SCP. Shared building blocks (rate
limiting, file locks, the `ConversationTracker`, OAuth/token helpers, retry and
structured logging) live in
[`gptmail.communication_utils`](src/gptmail/communication_utils/README.md).

## Development

```bash
cd packages/gptmail
make test        # pytest
make typecheck   # mypy
make format      # ruff
```

The tests in `tests/` are the best behaviour reference. For example,
`test_send_recipient_allowlist.py` covers the allowlist rules, and the
`test_agent_*_no_email_imports.py` tests enforce that `gptmail agent` never imports the
email stack. Historical design notes are in
[`src/gptmail/DESIGN.md`](src/gptmail/DESIGN.md).
