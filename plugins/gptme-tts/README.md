# gptme-tts

Text-to-speech for [gptme](https://gptme.org): the agent's replies are read aloud as they stream in. Speech comes from a local TTS server (Kokoro, KittenTTS or Chatterbox) or from OpenRouter speech models.

**Status:** experimental. Needs a working audio output device.

## Why use it

Hear what the agent says without watching the terminal, for example while it works through a long task. Speech starts sentence by sentence during streaming instead of waiting for the full reply, and code blocks, tool calls, markup and emoji are stripped before speaking.

For two-way, real-time conversations (microphone in, voice out, phone calls), see [gptme-voice](../../packages/gptme-voice/README.md) instead.

## Install

Install into the same Python environment as gptme. The package registers itself through the `gptme.plugins` entry point:

```sh
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tts"
# or, for a pipx-installed gptme:
pipx inject gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tts"
```

This adds a `tts` tool. The tool is only available when audio output works and a backend is reachable (see below). If you request it explicitly and it isn't available, gptme shows a hint about what is missing.

## Quickstart (local server, Kokoro)

`tts_server.py` in this directory is a self-contained [uv](https://docs.astral.sh/uv/) script; its dependencies are declared inline, so it needs no separate install.

```sh
# terminal 1: start the TTS server on 127.0.0.1:8765
cd /path/to/gptme-contrib/plugins/gptme-tts
./tts_server.py --backend kokoro

# terminal 2
gptme "hello, testing tts"
```

gptme checks for a server on `localhost:8765` at startup and enables speech if it finds one.

### Server options

| Option | Default | Meaning |
|--------|---------|---------|
| `--backend` | `kokoro` | `kokoro`, `kittentts` or `chatterbox` |
| `--voice` | backend default | Default voice (e.g. `af_heart` for Kokoro) |
| `--lang` | `a` | Kokoro language code (`a` = American English, `b` = British English, …) |
| `--voice-dir` | — | Directory of voice samples (Chatterbox) |
| `--list-voices` | — | List voices for the backend and exit |
| `--list-backends` | — | List backends whose dependencies are installed and exit |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address. The gptme plugin always connects to `localhost:8765`, so keep the default port. |
| `-v`, `--verbose` | — | Debug logging |

Backend notes:

- **Kokoro**: local, good quality, the default.
- **KittenTTS**: very small ONNX models; select a model with `TTS_MODEL` (default `KittenML/kitten-tts-micro-0.8`).
- **Chatterbox**: uses a hosted Gradio space (`ResembleAI/Chatterbox` by default, override with `GRADIO_SRC`); needs `HF_TOKEN`. Slower, so consider raising `GPTME_TTS_TIMEOUT`.

The server exposes `GET /tts?text=…&voice=…&speed=…` (returns WAV), `/health`, `/voices` and `/backends`.

## OpenRouter backend (no local server)

```sh
export GPTME_TTS_BACKEND=openrouter
export OPENROUTER_API_KEY=...       # or set it in gptme's config
gptme "read me a haiku"
```

The default model is `x-ai/grok-voice-tts-1.0` with voice `Ara`. Choose another with `GPTME_TTS_MODEL` and `GPTME_TTS_VOICE`.

## Environment variables

| Variable | Meaning |
|----------|---------|
| `GPTME_TTS_BACKEND` | `server` (default, local `tts_server.py`) or `openrouter` |
| `GPTME_TTS_VOICE` | Voice name (depends on backend/model) |
| `GPTME_TTS_SPEED` | Speed multiplier, `0.5`–`2.0` (default `1.0`) |
| `GPTME_TTS_TIMEOUT` | Per-request timeout in seconds (default `30`) |
| `GPTME_TTS_MODEL` | OpenRouter speech model when `GPTME_TTS_BACKEND=openrouter` |
| `GPTME_VOICE_FINISH` | `1`/`true`: wait for speech to finish before gptme exits |

## What the agent gets

Besides the automatic read-aloud hooks, the `tts` tool gives the agent these functions:

- `speak(text, block=False, interrupt=True, clean=True)`: say something specific
- `set_speed(speed)`: `0.5`–`2.0`
- `set_volume(volume)`: `0.0`–`1.0`
- `stop()`: stop current speech and clear the queue

Automatic read-aloud and `speak()` are both active, so an explicit `speak()` is heard in addition to the normal reply.

## Development

```sh
# from the gptme-contrib repo root
uv run pytest plugins/gptme-tts/tests
```
