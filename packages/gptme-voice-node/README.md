# gptme-voice-node

Thin embedded voice client for the [BobBrain](https://github.com/ErikBjare/bob/issues/730)
portable presence node. Connects to a running `bob-voice-server` WebSocket endpoint
and handles mic capture → server → speaker playback with echo prevention.

Designed for unattended embedded operation on a Raspberry Pi or any Linux host
with a USB microphone array (e.g. ReSpeaker XVF3800).

## Quick start (test on the runtime host, no Pi needed)

```bash
# Install
pip install gptme-voice-node   # or: uv pip install gptme-voice-node

# Run against a local voice server
GPTME_VOICE_NODE_SERVER=ws://localhost:8080/local \
GPTME_VOICE_NODE_NAME=bobbrain-puck \
gptme-voice-node
```

The node connects, plays audio from the server, and streams mic input back.
Press Ctrl-C to stop.

## Configuration

All config is via environment variables (no CLI flags — systemd-friendly):

| Variable | Default | Description |
|---|---|---|
| `GPTME_VOICE_NODE_SERVER` | `ws://localhost:8080/local` | WebSocket URL of `bob-voice-server` |
| `GPTME_VOICE_NODE_NAME` | `bobbrain-unknown` | Node identity used in local logs |
| `GPTME_VOICE_NODE_VISION_SOURCE` | unset | Optional `camera:N`, RTSP/HTTP stream URL, or image path; enables the camera bridge |
| `GPTME_VOICE_NODE_VISION_INTERVAL` | `1.0` | Seconds between local person/motion detection frames |

The `ws://localhost` default is intended only for a server on the same host. Use
`wss://` whenever microphone audio crosses a network; the client logs a warning
for non-local `ws://` endpoints.

## Raspberry Pi deployment

1. Install system deps:
   ```bash
   sudo apt install -y portaudio19-dev python3-pyaudio
   pip install gptme-voice-node
   ```

2. Flash ReSpeaker XVF3800 to USB audio firmware (see Seeed DFU docs).

3. Install the systemd unit:
   ```bash
   sudo cp systemd/gptme-voice-node.service /etc/systemd/system/
   # Customise server URL and node name:
   sudo mkdir -p /etc/systemd/system/gptme-voice-node.service.d/
   cat | sudo tee /etc/systemd/system/gptme-voice-node.service.d/local.conf << 'EOF'
   [Service]
   Environment=GPTME_VOICE_NODE_SERVER=wss://bob-host.example.com/local
   Environment=GPTME_VOICE_NODE_NAME=bobbrain-livingroom
   EOF
   sudo systemctl enable --now gptme-voice-node
   ```

4. Check logs:
   ```bash
   journalctl -u gptme-voice-node -f
   ```

## Protocol

The node speaks the same WebSocket JSON protocol as `gptme-voice-client`:

```
Client → Server: {"type": "audio", "audio": "<base64 PCM 16-bit 24kHz mono>"}
Client → Server: {"type": "vision_event", "event": "person_appeared", ...}
Client → Server: {"type": "vision_look_result", "request_id": "...", "image": "<base64 JPEG>"}
Server → Client: {"type": "audio", "audio": "<base64>"}
Server → Client: {"type": "audio_end"}
Server → Client: {"type": "vision_look_request", "request_id": "..."}
```

With the `vision` extra installed, set `GPTME_VOICE_NODE_VISION_SOURCE=camera:0`
to run cheap person/motion detectors on the node. Events carry metadata only. A
JPEG crosses the WebSocket only when the realtime model calls `look`; the host
then runs VLM inference and returns the description to the same voice turn.
The node advertises camera capability on connect, so camera-less `/local`
clients do not expose a `look` tool that can only time out. Vision events remain
telemetry in v0 and never trigger unsolicited model speech.

## Architecture

See `knowledge/technical-designs/bobbrain-spec.md` for the full BobBrain
architecture, including the BodyAdapter interface for mobility integration.

## Related

- `gptme-contrib/packages/gptme-voice/` — server and existing client (development reference)
- `knowledge/technical-designs/bobbody-voice-node-spec.md` — detailed protocol and Pi setup
- ErikBjare/bob#730 — BobBody / BobBrain tracking issue
