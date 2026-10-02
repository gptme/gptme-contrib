# gptme-voice-node

Headless voice client for an embedded agent "presence node", such as a Raspberry
Pi with a USB microphone array. It streams microphone audio to a
[gptme-voice](../gptme-voice/README.md) server, plays the agent's spoken reply,
and reconnects on its own. It runs unattended under systemd.

**Status:** experimental. Built for one agent's presence-node hardware prototype.
It works with any `gptme-voice-server`, but the protocol may still change.

## How it fits

```text
mic ─▶ gptme-voice-node ──WebSocket /local──▶ gptme-voice-server ──▶ OpenAI / xAI Realtime
speaker ◀─┘          ▲                              │
                     └── optional camera bridge ────┘ (look tool → host-side VLM)
```

- [gptme-voice](../gptme-voice/README.md) is the server. It holds the realtime
  session, agent personality and tools. This package is only the thin edge
  client.
- `gptme-voice-client` (from gptme-voice) is the desktop test client that speaks
  the same protocol. Use this package for unattended embedded devices.
- [gptme-vision-node](../gptme-vision-node/README.md) is an optional `vision`
  extra that adds a camera and an on-demand `look` tool.

Design points: the only core dependency is `websockets`. The mic is muted while
audio plays, plus a 0.5 s cooldown, to prevent echo on devices without hardware
AEC. Reconnects use exponential backoff (1 s up to 60 s).

## Install

None of these packages are on PyPI. Install from the monorepo, with the `audio`
extra (PyAudio) needed to actually capture and play sound:

```bash
# Debian / Raspberry Pi OS: PortAudio headers for PyAudio
sudo apt install -y portaudio19-dev

pip install "gptme-voice-node[audio] @ git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-voice-node"
```

For the camera bridge, install `gptme-vision-node` first, then the `vision`
extra:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-vision-node"
pip install "gptme-voice-node[audio,vision] @ git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-voice-node"
```

From a gptme-contrib checkout you can instead run
`uv sync --package gptme-voice-node --extra audio`.

## Quickstart (no Pi needed)

Start a voice server, then point the node at it:

```bash
gptme-voice-server                        # from gptme-voice; serves ws://…:8080/local

GPTME_VOICE_NODE_SERVER=ws://localhost:8080/local \
GPTME_VOICE_NODE_NAME=test-node \
gptme-voice-node
```

The node connects, streams mic input and plays the reply. Press Ctrl-C to stop.

## Configuration

Configuration is by environment variable only (no CLI flags), which keeps it
systemd-friendly:

| Variable | Default | Description |
|---|---|---|
| `GPTME_VOICE_NODE_SERVER` | `ws://localhost:8080/local` | WebSocket URL of the `gptme-voice-server` `/local` endpoint |
| `GPTME_VOICE_NODE_NAME` | `bobbrain-unknown` | Node identity used in logs |
| `GPTME_VOICE_NODE_VISION_SOURCE` | unset | `camera:N`, an RTSP/HTTP stream URL, or an image path. Enables the camera bridge (needs the `vision` extra) |
| `GPTME_VOICE_NODE_VISION_INTERVAL` | `1.0` | Seconds between on-node person/motion detection frames |

Use `wss://` whenever microphone audio crosses a network. The node logs a
warning for non-local `ws://` URLs and redacts credentials embedded in the URL.

## Raspberry Pi deployment

1. Install as above. If you use a ReSpeaker XVF3800 array, flash it to USB
   audio firmware (see Seeed's DFU docs).
2. Install the bundled systemd unit
   ([`systemd/gptme-voice-node.service`](./systemd/gptme-voice-node.service)).
   It assumes user `pi` and `~/.local/bin/gptme-voice-node`, so edit it if
   yours differ.

   ```bash
   sudo cp systemd/gptme-voice-node.service /etc/systemd/system/
   sudo mkdir -p /etc/systemd/system/gptme-voice-node.service.d/
   sudo tee /etc/systemd/system/gptme-voice-node.service.d/local.conf <<'EOF'
   [Service]
   Environment=GPTME_VOICE_NODE_SERVER=wss://voice.example.com/local
   Environment=GPTME_VOICE_NODE_NAME=livingroom
   EOF
   sudo systemctl daemon-reload
   sudo systemctl enable --now gptme-voice-node
   journalctl -u gptme-voice-node -f
   ```

   The unit restarts automatically and caps memory at 128 MB and CPU at 50%.

## Protocol

JSON over the WebSocket, the same as `gptme-voice-client`:

```text
Client → Server: {"type": "audio", "audio": "<base64 PCM 16-bit 24 kHz mono>"}
Client → Server: {"type": "vision_event", ...}
Client → Server: {"type": "vision_look_result", "request_id": "...", ...}
Server → Client: {"type": "audio", "audio": "<base64>"}
Server → Client: {"type": "audio_end"}
Server → Client: {"type": "vision_look_request", "request_id": "..."}
```

With a vision source configured, the node appends `vision=1` to the connect URL
so the server only exposes the `look` tool to camera-equipped clients. Events
carry metadata only. A JPEG crosses the socket only when the realtime model
calls `look`, and the host runs VLM inference. In v0, vision events are
telemetry and never trigger unsolicited speech.
