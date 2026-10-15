---
name: hold-to-talk
description: "Experimental single-turn voice input for gptme: capture or read audio, transcribe locally with faster-whisper, and optionally speak the response."
license: MIT
compatibility: "Python 3.12+, gptme with JSON output; microphone mode needs PortAudio and keyboard mode needs pynput"
category: "Agent Infrastructure"
keywords:
  - hold-to-talk
  - push to talk
  - whisper stt
metadata:
  version: "0.2.0"
  tags: [voice, stt, tts, gptme, desktop]
---

# Hold-to-talk

Run **one voice turn** through the gptme CLI. Invoke again for another turn;
this is not a persistent conversation or a Tauri integration.

Start with the [reproducible demo](demo.md): setup, architecture, an observed
file-input transcript, and the desktop verification checklist are there.
The file-input path works without a microphone or keyboard hook. Live device
capture and playback remain experimental and need testing on the target desktop.

## Usage

From this skill directory, with dependencies installed:

```sh
# Hold Space, speak, release. Needs a desktop audio device and keyboard access.
python3 scripts/hold-to-talk.py

# Fixed recording window; does not import pynput.
python3 scripts/hold-to-talk.py --duration 5

# Existing audio file; does not import sounddevice or pynput.
python3 scripts/hold-to-talk.py --audio-file sample.wav --no-tts

# Inspect transcription without starting gptme.
python3 scripts/hold-to-talk.py --audio-file sample.wav --transcript-only
```

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `HTT_MODEL` / `--model` | `tiny.en` | Local Whisper model; English-only by default |
| `HTT_HOLD_KEY` | `space` | pynput key name or one character |
| `HTT_GPTME_ARGS` | empty | Shell-quoted options passed before the positional prompt |
| `HTT_TTS_CMD` | auto-detect | Shell-quoted executable/options; response is appended as one argument |

Default gptme arguments are `-n --output-format json --tools none`. Text is
passed after `--` so a transcript beginning with a dash stays prompt text.
Non-interactive gptme skips confirmations. If you deliberately enable tools
through `HTT_GPTME_ARGS`, those tools run with that behavior.

STT runs locally after the model's first download. The transcript goes to the
configured gptme provider; use a local provider if it must remain on-device.
The offline demo uses gptme's canned `mock/static` response and makes no model
API call. gptme retains its normal conversation logs. Temporary recordings are
deleted after transcription, including transcription failures; supplied files
are retained. Status messages and the transcript appear on stdout.

TTS uses `say` on macOS or `espeak`/`espeak-ng` on Linux, if installed. Without
one, the response is printed. Use `--no-tts` to skip playback explicitly.
CLI errors and missing assistant output fail visibly rather than speaking
raw JSON or returning an empty answer. The gptme subprocess has a 60-second
timeout. No latency target has been established by desktop measurement.
