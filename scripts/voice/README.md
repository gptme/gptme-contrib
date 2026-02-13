# Voice Interface MVP for gptme

A minimal voice interface that allows interacting with gptme via voice.

## Features

- **Local Testing**: Works with microphone and speaker (no phone number needed)
- **Speech-to-Text**: Uses OpenAI Whisper API
- **Text-to-Speech**: Uses system TTS (espeak on Linux, say on macOS)
- **gptme Integration**: Processes voice input through gptme CLI

## Installation

```bash
# Install required packages
pip install pyaudio openai

# On Linux, you may need:
sudo apt-get install portaudio19-dev espeak
```

## Usage

### Basic Voice Loop

```bash
python voice_interface.py
```

This will:
1. Record audio for 5 seconds
2. Transcribe using Whisper
3. Process through gptme
4. Speak the response

### Test TTS

```bash
python voice_interface.py --test-tts
```

### Test STT (Speech-to-Text)

```bash
python voice_interface.py --test-stt
```

### Custom Recording Duration

```bash
python voice_interface.py --duration 10
```

## Requirements

- **pyaudio**: For microphone input
- **openai**: For Whisper STT API
- **espeak** (Linux) or **say** (macOS): For TTS

## Future: Twilio Integration

This MVP provides the foundation for phone-based voice interface. Future work:

1. Add Twilio webhook handler
2. WebSocket streaming for real-time audio
3. Integration with gptme server API

## Architecture

```txt
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Microphone  │────▶│   Whisper   │────▶│   gptme     │
│  (PyAudio)  │     │    (STT)    │     │    CLI      │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                                               ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Speaker   │◀────│    TTS      │◀────│  Response   │
│  (espeak)   │     │             │     │             │
└─────────────┘     └─────────────┘     └─────────────┘
```

## Related

- Issue: https://github.com/gptme/gptme-contrib/issues/266
