"""Tests for the vision pipeline event loop (synthetic source/detectors)."""

from __future__ import annotations

import threading

import numpy as np
import pytest
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


def test_latest_frame_copy_is_an_independent_snapshot():
    frame = solid_frame((60, 60, 60))
    pipeline = VisionPipeline(ListSource([frame]), [])

    pipeline.step()
    snapshot = pipeline.latest_frame_copy()
    assert snapshot is not None
    snapshot[:] = 0

    assert pipeline.latest_frame is not None
    assert np.any(pipeline.latest_frame != 0)


def test_process_frame_stores_an_independent_copy():
    """OpenCV can reuse the capture buffer; stored latest_frame must not alias it."""
    frame = solid_frame((60, 60, 60))
    pipeline = VisionPipeline(ListSource([frame]), [])

    pipeline.step()
    frame[:] = 0

    assert pipeline.latest_frame is not None
    assert np.any(pipeline.latest_frame != 0)


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


def test_callback_can_stop_pipeline():
    stopped = threading.Event()
    pipeline = None

    def stop_on_person(event):
        assert pipeline is not None
        pipeline.stop()
        stopped.set()

    pipeline = VisionPipeline(
        ListSource(_frames(100)),
        [ScriptedPersonDetector([True])],
        interval_s=0.01,
        on_event=stop_on_person,
    )
    pipeline.start()

    assert stopped.wait(timeout=1)
    thread = pipeline._thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
    pipeline.stop()
    assert pipeline._thread is None


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
    thread = pipeline._thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
    pipeline.stop()


def test_live_source_retries_after_empty_frame():
    frame_seen = threading.Event()

    class TransientLiveSource:
        stop_on_empty = False

        def __init__(self):
            self.calls = 0

        def get_frame(self):
            self.calls += 1
            if self.calls == 1:
                return None
            if self.calls == 2:
                return solid_frame((60, 60, 60))
            return None

    class SeenDetector:
        def detect(self, _frame):
            frame_seen.set()
            return []

    pipeline = VisionPipeline(TransientLiveSource(), [SeenDetector()], interval_s=0.01)
    pipeline.start()
    try:
        assert frame_seen.wait(timeout=1)
        assert pipeline._thread is not None
        assert pipeline._thread.is_alive()
    finally:
        pipeline.stop()


def test_start_resets_presence_state_after_restart():
    appeared = threading.Event()
    pipeline = VisionPipeline(
        ListSource(_frames(1)),
        [ScriptedPersonDetector([True])],
        interval_s=0.01,
        on_event=lambda event: appeared.set()
        if event.kind == PERSON_APPEARED
        else None,
    )
    pipeline.start()
    assert appeared.wait(timeout=1)
    first_thread = pipeline._thread
    assert first_thread is not None
    first_thread.join(timeout=1)
    assert not first_thread.is_alive()

    appeared.clear()
    pipeline.source = ListSource(_frames(1))
    pipeline.detectors = [ScriptedPersonDetector([True])]
    pipeline.start()

    assert appeared.wait(timeout=1)
    pipeline.stop()


def test_start_clears_stale_frame_until_new_capture():
    """Reconnect/restart must not serve a frame from the previous session."""
    first = solid_frame((10, 20, 30))
    pipeline = VisionPipeline(ListSource([first]), [], interval_s=0.01)
    pipeline.start()
    thread = pipeline._thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert pipeline.latest_frame is not None

    class EmptyLiveSource:
        stop_on_empty = False

        def get_frame(self):
            return None

    pipeline.source = EmptyLiveSource()
    pipeline.start()
    try:
        assert pipeline.latest_frame_copy() is None
    finally:
        pipeline.stop()


def test_stop_keeps_reference_to_blocked_thread():
    unblock = threading.Event()

    class BlockingSource:
        def get_frame(self):
            unblock.wait()
            return None

    pipeline = VisionPipeline(BlockingSource(), [])
    pipeline.start()
    thread = pipeline._thread
    assert thread is not None

    try:
        pipeline.stop(timeout=0.01)
        assert pipeline._thread is thread
        assert thread.is_alive()
        with pytest.raises(RuntimeError, match="already running"):
            pipeline.start()
    finally:
        unblock.set()
        thread.join(timeout=1)
        pipeline.stop()

    assert pipeline._thread is None
