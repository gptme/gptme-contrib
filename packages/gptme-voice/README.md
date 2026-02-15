# gptme-voice

Voice interface for gptme agents using OpenAI Realtime API.

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

# With debug logging
gptme-voice-server --debug

# Explicit workspace
gptme-voice-server --workspace /path/to/agent-repo
```

The server auto-detects the agent repo by walking up from gptme-contrib to find `gptme.toml`, and loads personality files (prioritizing ABOUT.md).

### Connect with local test client

```bash
# In a separate terminal
gptme-voice-test
```

Speak into your microphone. The agent responds with its configured personality and can use the subagent tool to interact with its workspace.

**Tip:** Use headphones to enable interrupting the agent mid-sentence (see Limitations below).

### API key

The OpenAI API key is loaded from gptme config (`~/.config/gptme/config.toml` or `config.local.toml`). No need to set `OPENAI_API_KEY` as an env var if it's already configured in gptme.

## Architecture

- **openai_client.py** - WebSocket client for OpenAI Realtime API with VAD, audio streaming, and event handling
- **server.py** - Starlette WebSocket server bridging clients to OpenAI
- **tool_bridge.py** - Async subagent dispatcher (runs `gptme --non-interactive` in background, injects results)
- **audio.py** - Audio format conversion (PCM ↔ μ-law for Twilio)
- **local_test.py** - Local test client with mic/speaker I/O and feedback loop prevention

## Limitations

- **No interruption without headphones.** The local test client mutes the mic while audio is playing to prevent feedback loops (speaker → mic → infinite loop). This means you can't interrupt the agent mid-sentence when using speakers. Use headphones to avoid this — with headphones there's no speaker bleed into the mic, so the client could skip muting. A proper fix would be acoustic echo cancellation (AEC), e.g. via `speexdsp` or WebRTC AEC.
- **Subagent latency.** Tool calls dispatch a full gptme subprocess, which takes a few seconds. The voice conversation continues while it runs, and the result is injected when ready.
