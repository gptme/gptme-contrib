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

Optional hardening:

```bash
TWILIO_ALLOWED_CALLERS=+46765784797,+15551234567
```

When set, only those caller numbers can get past `/incoming`. The server also
injects the caller number and Twilio caller name into the realtime instructions
for that specific call, so the agent can acknowledge who is calling without
guessing.

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
- **tool_bridge.py** - Async subagent dispatcher (runs `gptme --non-interactive` in background, injects results)
- **audio.py** - Audio format conversion (PCM ↔ μ-law for Twilio)
- **client.py** - Local client with mic/speaker I/O and feedback loop prevention

## Limitations

- **No interruption without headphones.** The local test client mutes the mic while audio is playing to prevent feedback loops (speaker → mic → infinite loop). This means you can't interrupt the agent mid-sentence when using speakers. Use headphones to avoid this — with headphones there's no speaker bleed into the mic, so the client could skip muting. A proper fix would be acoustic echo cancellation (AEC), e.g. via `speexdsp` or WebRTC AEC.
- **Subagent latency.** Tool calls dispatch a full gptme subprocess, which takes a few seconds. The voice conversation continues while it runs, and the result is injected when ready.
