"""The capture -> detect -> event loop.

Thread-based (the node targets a Pi; one capture thread at ~1 Hz is
plenty and keeps the package free of async plumbing at v0). The pipeline
keeps ``latest_frame`` around so the LLM look tool can answer "what do
you see right now?" without a second capture path.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .detect import Detection, Detector

logger = logging.getLogger(__name__)

# Event kinds
PERSON_APPEARED = "person_appeared"
PERSON_LEFT = "person_left"
MOTION = "motion"


@dataclass(frozen=True)
class VisionEvent:
    """Something noteworthy the pipeline saw."""

    kind: str  # person_appeared | person_left | motion
    detections: tuple[Detection, ...] = ()
    timestamp: float = field(default_factory=time.time)


EventCallback = Callable[[VisionEvent], None]


class VisionPipeline:
    """Capture frames, run detectors, emit events.

    - ``person_appeared`` / ``person_left``: edge-triggered on any
      ``person`` detections being present, debounced by
      ``absent_frames_for_left`` consecutive empty frames.
    - ``motion``: emitted whenever any ``motion`` detections fire.
    """

    def __init__(
        self,
        source,
        detectors: list[Detector],
        *,
        interval_s: float = 1.0,
        on_event: EventCallback | None = None,
        absent_frames_for_left: int = 3,
    ) -> None:
        self.source = source
        self.detectors = detectors
        self.interval_s = interval_s
        self.on_event = on_event
        self.absent_frames_for_left = absent_frames_for_left

        self.latest_frame: np.ndarray | None = None
        self._person_present = False
        self._frames_without_person = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- core -------------------------------------------------------------

    def _emit(self, event: VisionEvent) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            logger.exception("event callback failed for %s", event.kind)

    def _process_frame(self, frame: np.ndarray) -> list[VisionEvent]:
        """Run detectors and emit events for one captured frame."""
        self.latest_frame = frame

        detections: list[Detection] = []
        for detector in self.detectors:
            try:
                detections.extend(detector.detect(frame))
            except Exception:
                logger.exception("detector %r failed", detector)

        events: list[VisionEvent] = []
        persons = tuple(d for d in detections if d.kind == "person")
        motions = tuple(d for d in detections if d.kind == "motion")

        if persons:
            self._frames_without_person = 0
            if not self._person_present:
                self._person_present = True
                events.append(VisionEvent(PERSON_APPEARED, persons))
        elif self._person_present:
            self._frames_without_person += 1
            if self._frames_without_person >= self.absent_frames_for_left:
                self._person_present = False
                events.append(VisionEvent(PERSON_LEFT))

        if motions:
            events.append(VisionEvent(MOTION, motions))

        for event in events:
            self._emit(event)
        return events

    def step(self) -> list[VisionEvent]:
        """One iteration: capture, detect, emit. Returns emitted events."""
        frame = self.source.get_frame()
        return [] if frame is None else self._process_frame(frame)

    # -- thread lifecycle --------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                frame = self.source.get_frame()
                if frame is None:
                    break
                self._process_frame(frame)
            except Exception:
                logger.exception("pipeline step failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.interval_s - elapsed))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("pipeline already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="vision-pipeline", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None
