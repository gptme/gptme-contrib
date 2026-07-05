# gptme-whatsapp

WhatsApp integration for gptme agents. Enables agents (like Sven) to communicate via WhatsApp using [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).

Supports both **gptme** and **Claude Code** as agent backends.

## Architecture

```
WhatsApp (user's phone)
    ↕
whatsapp-web.js (Node.js bridge)
    ↕  spawns (per message)
gptme -p "<msg>" ...   OR   claude -p "<msg>" ...
    ↕
Response sent back to WhatsApp
```

Conversation history is maintained per-sender via named conversations (gptme) or `--resume` (Claude Code).

## Setup

### Prerequisites

- Node.js >= 18
- A phone/SIM for the agent (or link as secondary device on an existing phone)
- **gptme** or **Claude Code** installed in the agent's environment

### 1. Install Node.js dependencies

```bash
cd packages/gptme-whatsapp/node
npm install
```

Or use the Python helper:

```bash
pip install -e .
gptme-whatsapp-setup install
```

### 2. First run (QR code scan)

```bash
cd node
GPTME_AGENT=sven AGENT_WORKSPACE=/home/sven/sven node index.js
```

A QR code will print in the terminal. Scan it with the WhatsApp phone (Settings > Linked Devices > Link a device). Auth persists in `.wwebjs_auth/` — no rescanning needed after.

### 3. Configure backend and allowed contacts

**With gptme (default):**
```bash
GPTME_AGENT=sven ALLOWED_CONTACTS=447700900000 node index.js
```

**With Claude Code:**
```bash
# First, generate the system prompt
cd /home/sven/sven
./scripts/build-system-prompt.sh > state/system-prompt.txt

# Then run the bridge
BACKEND=claude-code GPTME_AGENT=sven ALLOWED_CONTACTS=447700900000 node index.js
```

Phone numbers in international format without `+`. If not set, all contacts are accepted.

### 4. Systemd service

Generate a service file:

```bash
gptme-whatsapp-setup service \
  --agent sven \
  --workspace /home/sven/sven \
  --contacts 447700900000 \
  --backend claude-code \
  --node-path /home/sven/.nvm/versions/node/v24.13.1/bin \
  > ~/.config/systemd/user/sven-whatsapp.service

systemctl --user daemon-reload
systemctl --user enable --now sven-whatsapp.service
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GPTME_AGENT` | `sven` | Agent name (used in conversation naming) |
| `BACKEND` | `gptme` | Agent backend: `gptme` or `claude-code` |
| `AGENT_WORKSPACE` | `~/<agent>` | Path to agent's workspace |
| `ALLOWED_CONTACTS` | `""` (all) | Comma-separated phone numbers (no `+`) |
| `GPTME_CMD` | `gptme` | Path to gptme binary |
| `CLAUDE_CMD` | `claude` | Path to claude binary |
| `SYSTEM_PROMPT_FILE` | `<workspace>/state/system-prompt.txt` | System prompt file for Claude Code backend |

## How it works

1. whatsapp-web.js connects to WhatsApp Web protocol (no official API needed)
2. On incoming message from an allowed contact, spawns the configured backend:
   - **gptme**: `gptme -p "<message>" --name "whatsapp-sven-<sender>" --non-interactive -y`
   - **Claude Code**: `claude -p "<message>" --output-format text --resume "whatsapp-sven-<sender>"`
3. The conversation name/resume ID persists history between messages
4. Response is sent back via WhatsApp

## Notes

- **Unofficial API**: whatsapp-web.js reverse-engineers the WhatsApp Web protocol. WhatsApp may occasionally break this with updates.
- **Rate limiting**: WhatsApp may rate-limit or ban accounts that send many automated messages. Keep to personal use.
- **QR code re-auth**: If auth expires, delete `.wwebjs_auth/` and re-scan.
- **Puppeteer**: whatsapp-web.js uses Puppeteer (headless Chrome). On low-memory servers, may need swap.
- **Claude Code stdin**: The bridge closes stdin immediately to prevent SIGSTOP in non-interactive contexts.
