"""Tests for the vision pipeline event loop (synthetic source/detectors)."""

from __future__ import annotations

import numpy as np
from gptme_vision_node.detect import Detection, MotionDetector
from gptme_vision_node.pipeline import (
    MOTION,
    PERSON_APPEARED,
    PERSON_LEFT,
    VisionPipeline,
)

from .conftest import solid_frame


class ListSource:
    """Feeds a scripted list of frames, then None."""

    def __init__(self, frames):
        self.frames = list(frames)

    def get_frame(self):
        return self.frames.pop(0) if self.frames else None


class ScriptedPersonDetector:
    """Returns a person detection when the script says so."""

    def __init__(self, script):
        self.script = list(script)

    def detect(self, frame):
        present = self.script.pop(0) if self.script else False
        if present:
            return [Detection(kind="person", box=(10, 10, 40, 80), score=1.0)]
        return []


def _frames(n):
    return [solid_frame((100, 100, 100)) for _ in range(n)]


def test_person_appeared_and_left_events():
    script = [
        False,
        True,
        True,
        False,
        False,
        False,
    ]  # left after 3 absent (debounce=2)
    pipeline = VisionPipeline(
        ListSource(_frames(len(script))),
        [ScriptedPersonDetector(script)],
        absent_frames_for_left=2,
    )
    events = []
    for _ in range(len(script)):
        events.extend(pipeline.step())
    kinds = [e.kind for e in events]
    assert kinds == [PERSON_APPEARED, PERSON_LEFT]


def test_person_appeared_only_once_while_present():
    script = [True, True, True]
    pipeline = VisionPipeline(ListSource(_frames(3)), [ScriptedPersonDetector(script)])
    events = []
    for _ in range(3):
        events.extend(pipeline.step())
    assert [e.kind for e in events] == [PERSON_APPEARED]
    assert events[0].detections[0].kind == "person"


def test_motion_events_and_latest_frame():
    still = solid_frame((60, 60, 60))
    moved = still.copy()
    moved[50:150, 50:250] = (255, 255, 255)
    pipeline = VisionPipeline(ListSource([still, moved]), [MotionDetector()])

    assert pipeline.latest_frame is None
    assert pipeline.step() == []  # primes background
    events = pipeline.step()
    assert [e.kind for e in events] == [MOTION]
    assert pipeline.latest_frame is not None
    assert np.array_equal(pipeline.latest_frame, moved)


def test_callback_receives_events_and_errors_are_contained():
    received = []

    def flaky_callback(event):
        received.append(event)
        raise RuntimeError("subscriber bug must not kill the pipeline")

    pipeline = VisionPipeline(
        ListSource(_frames(1)),
        [ScriptedPersonDetector([True])],
        on_event=flaky_callback,
    )
    events = pipeline.step()  # must not raise
    assert len(events) == 1
    assert received == events


def test_exhausted_source_yields_no_events():
    pipeline = VisionPipeline(ListSource([]), [MotionDetector()])
    assert pipeline.step() == []


def test_thread_lifecycle():
    pipeline = VisionPipeline(
        ListSource(_frames(100)), [MotionDetector()], interval_s=0.01
    )
    pipeline.start()
    try:
        import time

        time.sleep(0.1)
        assert pipeline.latest_frame is not None
    finally:
        pipeline.stop()
    assert pipeline._thread is None


def test_thread_stops_when_source_is_exhausted():
    pipeline = VisionPipeline(ListSource(_frames(1)), [], interval_s=0.01)
    pipeline.start()
    assert pipeline._thread is not None
    pipeline._thread.join(timeout=1)
    assert not pipeline._thread.is_alive()
    pipeline.stop()
