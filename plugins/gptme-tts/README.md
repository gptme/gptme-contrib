# gptme-tts

Text-to-speech (TTS) plugin for gptme using Kokoro.

## Installation

```bash
pip install gptme-tts
```

## Usage

Start the TTS server (included in the plugin directory):

```bash
# tts_server.py is a uv script with inline dependencies — run directly
./tts_server.py
```

Then start gptme normally — it will detect the running server and enable TTS automatically.

## Environment Variables

- `GPTME_TTS_VOICE`: Voice to use (depends on server backend, e.g. `am_adam`)
- `GPTME_TTS_SPEED`: Playback speed multiplier (default `1.0`)
- `GPTME_VOICE_FINISH`: Set to `1` to wait for speech to finish before exiting

## Design Notes

### Speak modes

The plugin exposes two ways to trigger speech:

- **Hook mode** (default): A `generation_post` hook automatically speaks every assistant message.
- **Explicit mode**: The `speak()` tool function lets the agent speak specific text on demand.

Currently both are active simultaneously, which can be redundant. A future version may let users configure one or the other via an env var (e.g. `GPTME_TTS_MODE=explicit` to disable the hook).
If you have a use case that requires one mode over the other, please open an issue.

### TTS server

`tts_server.py` is a standalone uv script with inline dependencies (no separate install needed). In a future version it may be started transparently when TTS is first used, rather than requiring manual startup.
