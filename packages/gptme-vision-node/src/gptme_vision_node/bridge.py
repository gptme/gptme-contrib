"""WebSocket bridge between a BobBrain camera and ``gptme-voice``.

The bridge owns the camera on the edge node, runs cheap person/motion
reflexes locally, and sends only compact event metadata unless the realtime
model explicitly calls the ``look`` tool.  A look request captures the latest
frame and sends one bounded JPEG over the existing voice-node WebSocket; VLM
inference remains on the runtime host.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable

import cv2

from .detect import Detection, Detector
from .frame_source import FrameSource
from .look import encode_frame_jpeg
from .pipeline import VisionEvent, VisionPipeline

logger = logging.getLogger(__name__)
_MAX_PENDING_EVENT_SENDS = 4

SendMessage = Callable[[str], Awaitable[None]]


async def _send_event(send_message: SendMessage, message: str) -> None:
    await send_message(message)


class VisionBridge:
    """Run a vision pipeline and exchange events/look results over a WebSocket."""

    def __init__(
        self,
        source: FrameSource,
        detectors: list[Detector],
        *,
        interval_s: float = 1.0,
        absent_frames_for_left: int = 3,
        jpeg_quality: int = 75,
    ) -> None:
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        self.jpeg_quality = jpeg_quality
        self.pipeline = VisionPipeline(
            source,
            detectors,
            interval_s=interval_s,
            on_event=self._on_event,
            absent_frames_for_left=absent_frames_for_left,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_message: SendMessage | None = None
        self._pending_sends: set[asyncio.Task[None]] = set()

    def _on_event(self, event: VisionEvent) -> None:
        """Forward a pipeline-thread event to the asyncio WebSocket loop."""
        loop = self._loop
        if loop is None or self._send_message is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._schedule_event, event)

    def _schedule_event(self, event: VisionEvent) -> None:
        send_message = self._send_message
        if send_message is None:
            return
        if len(self._pending_sends) >= _MAX_PENDING_EVENT_SENDS:
            logger.warning("dropping vision event while transport is backlogged")
            return
        message = json.dumps(
            {
                "type": "vision_event",
                "event": event.kind,
                "timestamp": event.timestamp,
                "detections": [self._serialize_detection(d) for d in event.detections],
            }
        )
        task: asyncio.Task[None] = asyncio.create_task(
            _send_event(send_message, message)
        )
        self._pending_sends.add(task)
        task.add_done_callback(self._event_send_done)

    def _event_send_done(self, task: asyncio.Task[None]) -> None:
        self._pending_sends.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("failed to send vision event")

    @staticmethod
    def _serialize_detection(detection: Detection) -> dict[str, object]:
        return {
            "kind": detection.kind,
            "box": list(detection.box),
            "score": detection.score,
        }

    async def handle_message(self, data: object, send_message: SendMessage) -> bool:
        """Handle a server message. Return true when it was a vision request."""
        if not isinstance(data, dict) or data.get("type") != "vision_look_request":
            return False

        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            logger.warning("ignoring vision look request without request_id")
            return True

        frame = self.pipeline.latest_frame_copy()
        if frame is None:
            payload: dict[str, object] = {
                "type": "vision_look_result",
                "request_id": request_id,
                "error": "Camera has not produced a frame yet.",
            }
        else:
            try:
                jpeg = await asyncio.to_thread(
                    encode_frame_jpeg, frame, quality=self.jpeg_quality
                )
                payload = {
                    "type": "vision_look_result",
                    "request_id": request_id,
                    "image": base64.b64encode(jpeg).decode("ascii"),
                    "media_type": "image/jpeg",
                }
            except (cv2.error, ValueError) as exc:
                payload = {
                    "type": "vision_look_result",
                    "request_id": request_id,
                    "error": f"Failed to encode camera frame: {exc}",
                }
        await send_message(json.dumps(payload))
        return True

    def start(self, loop: asyncio.AbstractEventLoop, send_message: SendMessage) -> None:
        """Attach a connected transport and start the capture pipeline."""
        self._loop = loop
        self._send_message = send_message
        self.pipeline.start()

    async def stop(self) -> None:
        """Stop capture, release the source, and drain pending event sends."""
        self.pipeline.stop()
        release = getattr(self.pipeline.source, "release", None)
        if release is not None:
            release()
        self._send_message = None
        self._loop = None
        pending = list(self._pending_sends)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending_sends.clear()
