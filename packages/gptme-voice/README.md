# gptme-voice

Voice interface for gptme with OpenAI Realtime API support.

## Features

- **Real-time voice conversations**: Low-latency voice interactions with gptme
- **Twilio integration**: Phone call support via Twilio Media Streams
- **Local testing**: Direct audio input/output for development
- **Tool access**: Voice conversations can use gptme tools

## Installation

```bash
pip install gptme-voice
```

## Usage

### Local Testing

```bash
# Set your OpenAI API key
export OPENAI_API_KEY=your-key-here

# Run local voice test
gptme-voice-test
```

### Twilio Server

```bash
# Start the WebSocket server for Twilio
gptme-voice-server --port 8080
```

## Architecture
