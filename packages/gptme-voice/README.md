# gptme-voice

Voice interface for gptme agents using OpenAI or xAI Grok Realtime APIs.

## Features

- **Real-time voice conversations** with low-latency audio streaming
- **Agent personality loading** from gptme.toml project config (ABOUT.md, etc.)
- **Subagent tool** dispatches tasks to gptme for workspace interaction (read files, check tasks, run commands)
- **Auto-detection** of agent repo when installed in gptme-contrib
- **Feedback loop prevention** by muting mic during playback
- **Twilio integration** for phone call support via Media Streams
- **Local testing** with direct microphone/speaker I/O
- **BobBrain camera tool** — the `/local` session exposes `look` and runs VLM
  inference only after the edge node returns an on-demand frame
- **Goal-level body tools** — in-process MAVSDK or an authenticated remote
  `bob-body/0` node can handle status, bounded movement, stop, turn, and local
  interaction directly in the realtime tool bridge (never via a subagent)

## Installation

```bash
# Install with poetry (from gptme-contrib)
cd packages/gptme-voice
poetry install

# For local mic/speaker testing
poetry install -E local
```

## Usage

### Start the server

```bash
# Auto-detects agent repo and loads personality
gptme-voice-server

# Use xAI Grok
gptme-voice-server --provider grok

# With debug logging
gptme-voice-server --debug

# Explicit workspace
gptme-voice-server --workspace /path/to/agent-repo
```

The server auto-detects the agent repo by walking up from gptme-contrib to find `gptme.toml`, and loads personality files (prioritizing ABOUT.md).

### Connect with local client

```bash
# In a separate terminal
gptme-voice-client
```

Speak into your microphone. The agent responds with its configured personality and can use the subagent tool to interact with its workspace.

**Tip:** Use headphones to enable interrupting the agent mid-sentence (see Limitations below).

### Receive phone calls via Twilio

1. Start the server with a public URL (e.g. via ngrok):
   ```bash
   gptme-voice-server --port 8080
   ngrok http 8080
   ```
2. In the Twilio console, set your phone number's **Voice webhook** to:
   `https://<your-ngrok-url>/incoming` (HTTP POST)
3. Call the Twilio number — Twilio connects the call to the voice server.

### Place outbound phone calls via Twilio

Set these values in your environment or gptme config:

```bash
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=...
GPTME_VOICE_PUBLIC_BASE_URL=https://<your-ngrok-url>
```

Then place a call:

```bash
gptme-voice-call +46701234567
```

Use `--dry-run` to print the generated TwiML without dialing.

### Connect a remote body node

Configure a private body endpoint and pass its token separately so credentials
do not appear in URLs or logs:

```bash
GPTME_VOICE_BODY_URL=tcp://127.0.0.1:7777
GPTME_VOICE_BODY_TOKEN=<body-node-token>
GPTME_VOICE_BODY_CONTROLLER_ID=gptme-voice-local  # optional
```

Plaintext `tcp://` is loopback-only (`127.0.0.1` / `::1`; hostnames including
`localhost` are refused). A non-loopback host is refused so the bearer token
and physical commands never cross the network in the clear.

The remote node negotiates its actual capabilities during the
controller-authenticated handshake. `gptme-voice` registers only the
corresponding realtime tools. The body node remains responsible for controller
leases, command TTLs, idempotency, deadman behavior, and collision/local
safety.

### API keys

Keys are loaded from gptme config (`~/.config/gptme/config.toml` or
`config.local.toml`):

- `OPENAI_API_KEY` for the default `openai` provider
- `XAI_API_KEY` for `--provider grok`

No need to export them as shell env vars if they're already configured in gptme.

## Architecture

- **openai_client.py** - WebSocket client for OpenAI Realtime API with VAD, audio streaming, and event handling
- **xai_client.py** - xAI Grok Voice Agent adapter (OpenAI-compatible WebSocket protocol)
- **server.py** - Starlette WebSocket server bridging clients to OpenAI or xAI
- **tool_bridge.py** - Async subagent dispatcher plus body/vision tool routing
- **vision.py** - Correlated camera-frame requests, edge-event handling, and host-side VLM inference
- **audio.py** - Audio format conversion (PCM ↔ μ-law for Twilio)
- **client.py** - Local client with mic/speaker I/O and feedback loop prevention

## Limitations

- **No interruption without headphones.** The local test client mutes the mic while audio is playing to prevent feedback loops (speaker → mic → infinite loop). This means you can't interrupt the agent mid-sentence when using speakers. Use headphones to avoid this — with headphones there's no speaker bleed into the mic, so the client could skip muting. A proper fix would be acoustic echo cancellation (AEC), e.g. via `speexdsp` or WebRTC AEC.
- **Subagent latency.** Tool calls dispatch a full gptme subprocess, which takes a few seconds. The voice conversation continues while it runs, and the result is injected when ready.
