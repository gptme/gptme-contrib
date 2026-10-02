# gptme-whatsapp

Let people chat with your gptme or Claude Code agent over WhatsApp. A small
Node.js bridge links a WhatsApp account as a "linked device"
([whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js)), runs the
agent once per incoming message, and sends the reply back.

**Status:** experimental. It is a working bridge used by a deployed agent, but
it relies on an unofficial WhatsApp Web client (see [Caveats](#caveats)). The
Python package is only a setup helper. The bridge itself is
[`node/index.js`](./node/index.js).

## How it fits

This is one of several communication channels in gptme-contrib. You can use it
alone or alongside others, or bring your own messaging system entirely:

- [gptmail](../gptmail/README.md) covers email and agent-to-agent messaging.
- [gptme-voice](../gptme-voice/README.md) covers real-time voice and phone calls.
- Discord, Telegram and other social integrations are listed in the
  [repo README](../../README.md).

```text
WhatsApp (phone) ⇄ whatsapp-web.js bridge (Node.js)
                       │ spawns per message
                       ▼
     gptme --name whatsapp-<agent>-<sender> --non-interactive -y -- "<msg>"
  or claude -p "<msg>" --output-format text --resume whatsapp-<agent>-<sender>
                       │
                       ▼
                reply sent back to the chat
```

With the gptme backend, history persists per sender through the named gptme
conversation. The Claude Code backend passes the same name to `--resume`;
Claude Code normally expects a session ID there, so check that per-sender
history (and replies at all) work with your Claude Code version before relying
on it. Group chats, status broadcasts and non-text messages
are ignored. Replies are truncated to fit WhatsApp's 4096-character limit, and a
backend run is killed after 120 s.

## Setup

### Prerequisites

- Node.js ≥ 18 and npm
- A WhatsApp account for the agent (a dedicated SIM, or link it as a secondary
  device on an existing phone)
- `gptme` or `claude` (Claude Code) installed for the user that runs the bridge

### 1. Install Node dependencies

```bash
cd packages/gptme-whatsapp/node
npm install
```

Or use the Python helper from a gptme-contrib checkout. It checks the Node
version and runs `npm install` in `node/`:

```bash
uv sync --package gptme-whatsapp   # or: pip install -e packages/gptme-whatsapp
gptme-whatsapp-setup install
```

The helper locates `node/` relative to its source file, so it only works from a
checkout (editable/workspace install), not from a plain wheel.

### 2. First run: scan the QR code

```bash
cd packages/gptme-whatsapp/node
GPTME_AGENT=myagent AGENT_WORKSPACE=~/myagent ALLOWED_CONTACTS=447700900000 node index.js
```

Scan the printed QR code from the agent's phone (WhatsApp → Settings → Linked
devices → Link a device). Auth is stored in `.wwebjs_auth/` in the working
directory, so later runs don't need a rescan.

**Always set `ALLOWED_CONTACTS`.** If it is empty, the bridge answers *every*
contact that messages the number, and each message runs your agent with tool
auto-approval (`-y`) in its workspace.

### 3. Choose a backend

```bash
# gptme (default)
BACKEND=gptme GPTME_AGENT=myagent ALLOWED_CONTACTS=447700900000 node index.js

# Claude Code: identity comes from a system-prompt text file you generate
# from your agent's core files (default: <workspace>/state/system-prompt.txt)
BACKEND=claude-code GPTME_AGENT=myagent ALLOWED_CONTACTS=447700900000 node index.js
```

Phone numbers are in international format without `+`, comma-separated.

### 4. Run as a systemd user service

```bash
gptme-whatsapp-setup service \
  --agent myagent \
  --workspace ~/myagent \
  --contacts 447700900000 --contacts 447700900001 \
  --backend gptme \
  --node-path "$(dirname "$(command -v node)")" \
  > ~/.config/systemd/user/myagent-whatsapp.service

systemctl --user daemon-reload
systemctl --user enable --now myagent-whatsapp.service
```

`service` options: `--agent` and `--workspace` (required), `--contacts`
(repeatable), `--backend gptme|claude-code`, `--node-path` (directory prepended
to `PATH`), and `--claude-path` (directory containing `claude`). The generated
unit's `ExecStart` calls `/usr/bin/node`. If Node lives elsewhere (for example
nvm), edit that line. The unit's `WorkingDirectory` is the package's `node/`
directory, so `.wwebjs_auth/` from your first run there is reused.

## Environment variables (bridge)

| Variable | Default | Description |
|---|---|---|
| `GPTME_AGENT` | `sven` | Agent name, used in conversation names. Set this explicitly |
| `BACKEND` | `gptme` | `gptme` or `claude-code` |
| `AGENT_WORKSPACE` | `$HOME/<agent>` | Agent workspace path |
| `ALLOWED_CONTACTS` | empty (accept all) | Comma-separated phone numbers, no `+` |
| `GPTME_CMD` | `gptme` | gptme binary |
| `CLAUDE_CMD` | `claude` | Claude Code binary |
| `SYSTEM_PROMPT_FILE` | `<workspace>/state/system-prompt.txt` | Passed to Claude Code via `--append-system-prompt-file` (only if it exists) |

## Caveats

- **Unofficial API**: whatsapp-web.js drives WhatsApp Web through headless
  Chrome (Puppeteer). WhatsApp updates can break it, and automated use can get
  an account rate-limited or banned. Keep it to personal, low-volume use.
- **Memory**: Puppeteer/Chrome needs a few hundred MB. Small servers may need
  swap.
- **Re-auth**: if the session expires, delete `.wwebjs_auth/` and rescan.
- **One process per message**: each message starts a fresh `gptme`/`claude`
  run, so replies take as long as a cold agent start.
- **Claude Code stdin**: The bridge closes stdin immediately to prevent SIGSTOP
  in non-interactive contexts.
