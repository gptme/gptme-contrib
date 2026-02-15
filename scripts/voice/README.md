# Voice Interface for gptme

A voice interface that allows interacting with gptme via voice, supporting both local testing and phone calls via Twilio.

## Features

- **Real-time Streaming**: Uses OpenAI Realtime API for low-latency (~300-500ms) conversations
- **Tool Integration**: Access to gptme tools via async subagent pattern
- **Twilio Integration**: Phone call support via Twilio Media Streams
- **Local Testing**: Works with microphone and speaker (no phone number needed)

## Architecture

```txt
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Phone Call     │────▶│  Twilio Media    │────▶│  Voice Server   │
│  (or Local)     │     │  Stream (WS)     │     │  (FastAPI)      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                        ┌─────────────────────────────────┘
                        ▼
                ┌──────────────────┐     ┌─────────────────┐
                │  OpenAI Realtime │────▶│  gptme Tools    │
                │  API (WS)        │     │  (async)        │
                └──────────────────┘     └─────────────────┘
```

## Installation

```bash
# Install required packages
pip install pyaudio openai websockets starlette uvicorn

# On Linux, you may also need:
sudo apt-get install portaudio19-dev
```

## Usage

### Start the Voice Server

```bash
# Start server (requires OPENAI_API_KEY env var)
cd gptme-contrib/scripts/voice
python -m realtime.server

# Or with custom options
python -m realtime.server --host 0.0.0.0 --port 8080 --workspace /path/to/workspace
```

### Local Testing

```bash
# Test with microphone and speaker
python -m realtime.local_test

# Connect to custom server
python -m realtime.local_test --server ws://localhost:8080/local
```

### Twilio Integration

1. **Set up Twilio**:
   - Buy a phone number in Twilio console
   - Configure the number's voice webhook to: `ws://your-server:8080/twilio`

2. **Make a call**:
   - Call your Twilio number
   - Speak naturally with the AI assistant

## Components

### `realtime/` - Real-time Voice Interface

| File | Purpose |
|------|---------|
| `__init__.py` | Module initialization |
| `audio.py` | Audio format conversion (μ-law ↔ PCM) |
| `openai_client.py` | OpenAI Realtime API WebSocket client |
| `tool_bridge.py` | Transform OpenAI function calls → gptme calls |
| `server.py` | FastAPI WebSocket server for Twilio/local |
| `local_test.py` | Local testing with microphone/speaker |

### `voice_interface.py` - MVP Voice Interface

The original MVP implementation using record → transcribe → process → TTS. Still useful for:
- Simple voice commands
- Non-real-time use cases
- Testing without OpenAI Realtime API access

## Requirements

- **pyaudio**: For microphone/speaker access
- **openai**: For OpenAI Realtime API
- **websockets**: For WebSocket communication
- **starlette**: For FastAPI-like server
- **uvicorn**: For ASGI server

## Environment Variables

- `OPENAI_API_KEY`: Required for OpenAI Realtime API

## Related

- Issue: https://github.com/gptme/gptme-contrib/issues/266
