# gptme-vision-node

Vision pipeline for the **BobBrain presence node** — the "eyes" half of the
portable brain payload (see `bobbrain-spec.md`, ErikBjare/bob#730). Milestone 2
(vision v0): periodic frame capture, cheap on-node detection reflexes, an
on-demand LLM "look" tool, and GPS-free place recognition.

Deliberately light dependencies: `opencv-python-headless`, `numpy`, `httpx`.
No torch/ultralytics/CLIP — all heavy inference is remote (that's the
architecture), and the on-node detectors are OpenCV built-ins.

## Components

| Module | What |
|---|---|
| `frame_source` | `FrameSource` protocol; `ImageFileSource` (file or dir round-robin), `OpenCVCameraSource` (V4L2 index / RTSP URL) |
| `detect` | `PersonDetector` (OpenCV HOG, no model downloads), `MotionDetector` (frame differencing vs running-average background) |
| `look` | `describe_frame(frame, prompt)` → vision LLM via any OpenAI-compatible endpoint (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `VISION_MODEL`) |
| `place` | "Where am I?": `HistogramEmbedder` (HSV + spatial grid baseline), `WifiSignature` (nmcli BSSID/RSSI fingerprints), `PlaceRecognizer` (fused enroll/recognize, JSON gallery) |
| `pipeline` | `VisionPipeline` — capture → detect → `VisionEvent` (person_appeared / person_left / motion) callback; keeps `latest_frame` for the look tool |
| `cli` | `gptme-vision-node {detect,look,enroll,whereami}` |

## Usage

```bash
# Detection on a webcam (or an image/dir for testing)
gptme-vision-node detect --source camera:0
gptme-vision-node detect --source photos/ --once

# Ask a vision LLM what's in view (needs OPENAI_API_KEY)
gptme-vision-node look --source camera:0 --prompt "Who is in the room?"

# Place recognition: enroll a few samples per room, then ask
gptme-vision-node enroll --place kitchen --source camera:0
gptme-vision-node enroll --place office --source camera:0
gptme-vision-node whereami --source camera:0
```

The place gallery defaults to `~/.config/gptme-vision-node/places.json`.
WiFi fingerprints (visible BSSIDs + signal) are captured automatically when
`nmcli` is available and fused with the visual score (`--no-wifi` to skip).

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

## Voice bridge

Install the vision extra next to the embedded voice node and point it at a
camera or stream:

```bash
pip install 'gptme-voice-node[audio,vision]'
GPTME_VOICE_NODE_VISION_SOURCE=camera:0 gptme-voice-node
```

The pipeline sends compact person/motion events over the existing `/local`
WebSocket. It keeps the latest frame on-node until the realtime session calls
`look`; only then is one JPEG sent to the host for VLM inference.
Events are telemetry only in v0; they do not trigger unsolicited model speech.

## Future (explicitly not v0)

- **CLIP embeddings** for place recognition — a strict upgrade over the
  histogram baseline (viewpoint/lighting invariance); would ship as an
  optional extra implementing the same `Embedder` protocol.
- On-demand live streaming (WebRTC/RTSP) to the host — vision v1 in the spec.

## Tests

```bash
uv run pytest packages/gptme-vision-node -q
```

Fully offline: synthetic numpy images, no camera, no network (LLM calls
mocked).
