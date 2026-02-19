# gptme-whatsapp

WhatsApp integration for gptme agents. Enables agents (like Sven) to communicate via WhatsApp using [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).

## Architecture

```
WhatsApp (user's phone)
    ↕
whatsapp-web.js (Node.js bridge)
    ↕  spawns
gptme -p "<message>" --name "whatsapp-<agent>-<sender>" --non-interactive
    ↕
Response sent back to WhatsApp
```

Conversation history is maintained per-sender via gptme's named conversations, so Sven "remembers" Tekla across messages.

## Setup

### Prerequisites

- Node.js >= 18
- A phone/SIM for the agent (or link as secondary device on an existing phone)
- gptme installed in the agent's environment

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

### 3. Configure allowed contacts

```bash
ALLOWED_CONTACTS=447700900000,447700900001 GPTME_AGENT=sven node index.js
```

Phone numbers in international format without `+`. If not set, all contacts are accepted.

### 4. Systemd service

Generate a service file:

```bash
gptme-whatsapp-setup service \
  --agent sven \
  --workspace /home/sven/sven \
  --contacts 447700900000 \
  --node-path /home/sven/.nvm/versions/node/v24.13.1/bin \
  > ~/.config/systemd/user/sven-whatsapp.service

systemctl --user daemon-reload
systemctl --user enable --now sven-whatsapp.service
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GPTME_AGENT` | `sven` | Agent name (used in conversation naming) |
| `AGENT_WORKSPACE` | `~/<agent>` | Path to agent's gptme workspace |
| `ALLOWED_CONTACTS` | `""` (all) | Comma-separated phone numbers (no `+`) |
| `GPTME_CMD` | `gptme` | Path to gptme binary |

## How it works

1. whatsapp-web.js connects to WhatsApp Web protocol (no official API needed)
2. On incoming message from an allowed contact, spawns:
   ```
   gptme -p "<message>" --name "whatsapp-sven-447700900000" --non-interactive -y
   ```
3. The conversation name persists history between messages
4. Response is sent back via WhatsApp

## Notes

- **Unofficial API**: whatsapp-web.js reverse-engineers the WhatsApp Web protocol. WhatsApp may occasionally break this with updates.
- **Rate limiting**: WhatsApp may rate-limit or ban accounts that send many automated messages. Keep to personal use.
- **QR code re-auth**: If auth expires, delete `.wwebjs_auth/` and re-scan.
- **Puppeteer**: whatsapp-web.js uses Puppeteer (headless Chrome). On low-memory servers, may need swap.
