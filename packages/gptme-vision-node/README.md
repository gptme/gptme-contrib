# gptme-vision-node

Lightweight camera pipeline for an embedded agent "presence node": capture
frames from a webcam, RTSP stream or image files, run cheap on-device
person/motion detection, describe the scene on demand with a vision LLM, and
recognise which room the device is in without GPS.

**Status:** experimental (v0). Built for one agent's presence-node hardware
prototype. It works on any Linux box with a camera, but the API may change.

Dependencies are deliberately light: `opencv-python-headless` (<5), `numpy`,
`httpx`, `click`. There is no torch/CLIP/YOLO. On-node detectors are OpenCV
built-ins (no model downloads), and heavy inference runs remotely through an
OpenAI-compatible API.

## How it fits

- **Standalone:** the `gptme-vision-node` CLI covers detection, scene
  description and place recognition.
- **With voice:** [gptme-voice-node](../gptme-voice-node/README.md) can run this
  pipeline next to the microphone and stream compact events to
  [gptme-voice](../gptme-voice/README.md). The realtime model can then call a
  `look` tool that sends one JPEG back to the host for VLM inference (see
  [Voice bridge](#voice-bridge)).

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-vision-node"
```

Or, from a gptme-contrib checkout: `uv sync --package gptme-vision-node`.

## Quickstart

`--source` accepts `camera:N` (V4L2 index), a stream URL (`rtsp://…`,
`http://…`), or an image file/directory (cycled round-robin, handy for
testing).

```bash
# Person/motion detection
gptme-vision-node detect --source camera:0
gptme-vision-node detect --source photos/ --once

# Describe the current frame with a vision LLM (needs OPENAI_API_KEY)
gptme-vision-node look --source camera:0 --prompt "Who is in the room?"

# Place recognition: enroll a few samples per room, then ask
gptme-vision-node enroll --place kitchen --source camera:0
gptme-vision-node enroll --place office --source camera:0
gptme-vision-node whereami --source camera:0 --json
```

## CLI reference

| Command | Options |
|---|---|
| `detect` | `--source`, `--once`, `--interval SECONDS` (default 1.0) |
| `look` | `--source`, `--prompt`, `--model` (overrides `VISION_MODEL`) |
| `enroll` | `--source`, `--place NAME`, `--gallery PATH`, `--no-wifi` |
| `whereami` | `--source`, `--gallery PATH`, `--no-wifi`, `--json` |

The place gallery defaults to `~/.config/gptme-vision-node/places.json`. When
`nmcli` is available, a WiFi fingerprint (visible BSSIDs + signal strength) is
captured with each sample and fused with the visual score. Use `--no-wifi` to
skip it.

### Vision LLM settings (`look`)

| Env var | Default |
|---|---|
| `OPENAI_API_KEY` | required |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` (any OpenAI-compatible endpoint) |
| `VISION_MODEL` | `gpt-4o-mini` |

## Python API

```python
from gptme_vision_node import VisionPipeline, ImageFileSource, PersonDetector, MotionDetector

pipeline = VisionPipeline(
    ImageFileSource("frames/"),
    [PersonDetector(), MotionDetector()],
    interval_s=1.0,
    on_event=lambda e: print(e.kind, e.detections),
)
pipeline.start()
```

| Module | Contents |
|---|---|
| `frame_source` | `FrameSource` protocol; `ImageFileSource` (file or directory), `OpenCVCameraSource` (V4L2 index or stream URL) |
| `detect` | `PersonDetector` (OpenCV HOG), `MotionDetector` (frame differencing against a running-average background) |
| `look` | `describe_frame(frame, prompt)`: one frame to a vision chat model |
| `place` | `HistogramEmbedder` (HSV + spatial-grid baseline), `WifiSignature` (nmcli fingerprints), `PlaceRecognizer` (enroll/recognize, JSON gallery) |
| `pipeline` | `VisionPipeline`: capture, then detect, then `VisionEvent` (`person_appeared` / `person_left` / `motion`). Keeps `latest_frame` for `look` |
| `bridge` | `VisionBridge`: runs the pipeline and exchanges events/look results over a voice-node WebSocket |

## Voice bridge

With [gptme-voice-node](../gptme-voice-node/README.md) installed with its
`vision` extra, point it at a camera:

```bash
GPTME_VOICE_NODE_VISION_SOURCE=camera:0 gptme-voice-node
```

The bridge sends only compact person/motion event metadata over the existing
`/local` WebSocket. It keeps the latest frame on the node until the realtime
session calls `look`, then sends one JPEG to the host for VLM inference. In v0
events are telemetry only and never trigger unsolicited speech. See the
[gptme-voice-node README](../gptme-voice-node/README.md) for installation.

## Not in v0

- CLIP embeddings for place recognition (better viewpoint/lighting
  invariance), as an optional extra implementing the same `Embedder` protocol.
- On-demand live streaming (WebRTC/RTSP) to the host.

## Tests

Tests are fully offline: synthetic numpy images, no camera, no network (LLM
calls mocked). From the repo root: `make -C packages/gptme-vision-node test`.
