# Voice Interface - Real-Time Streaming

Real-time voice interface for gptme using OpenAI's Realtime API.
Enables low-latency voice conversations with access to gptme tools.

## Architecture

Phone Call → Twilio → WebSocket Server → OpenAI Realtime API
                              ↓
                        Tool Bridge
                              ↓
                        gptme CLI (async)

## Components

- **audio.py**: Audio format conversion (Twilio μ-law 8kHz ↔ OpenAI PCM 24kHz)
- **openai_client.py**: OpenAI Realtime API WebSocket client
- **tool_bridge.py**: Async gptme tool execution
- **server.py**: FastAPI WebSocket server
- **local_test.py**: Local testing mode (microphone → speaker)

## Setup

1. Install dependencies:
   ```bash
   pip install fastapi uvicorn websockets pyaudio
   ```

2. Set environment variables:
   ```bash
   export OPENAI_API_KEY=your-key-here
   ```

## Usage

### Local Testing

Test without Twilio using microphone and speakers:

```bash
cd gptme-contrib/scripts/voice
python -m realtime.local_test
```

### Twilio Integration

1. Deploy the WebSocket server:
   ```bash
   uvicorn realtime.server:app --host 0.0.0.0 --port 8080
   ```

2. Configure Twilio phone number webhook to point to your server.

3. Call the phone number to interact with gptme via voice.

## Tool Bridge

The tool bridge allows the voice assistant to execute gptme tools:

- Spawns gptme subprocess for function calls
- Returns results asynchronously to the conversation
- Follows Erik's async subagent pattern

## Related

- Issue #266: Voice interface via phone calls
- Architecture doc: knowledge/technical-designs/voice-interface-realtime-architecture.md
