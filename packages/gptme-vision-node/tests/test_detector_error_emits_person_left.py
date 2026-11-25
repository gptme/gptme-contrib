"""A transient PersonDetector exception is counted as 'nobody in frame',
so a person who never left gets a person_left event."""

from __future__ import annotations

import numpy as np
from gptme_vision_node.detect import Detection, MotionDetector
from gptme_vision_node.pipeline import PERSON_APPEARED, PERSON_LEFT, VisionPipeline


class Frames:
    def get_frame(self):
        return np.zeros((32, 32, 3), dtype=np.uint8)


class FlakyPersonDetector:
    """Sees the person, then errors (e.g. cv2.error) for 3 frames."""

    def __init__(self) -> None:
        self.n = 0

    def detect(self, frame):
        self.n += 1
        if self.n == 1:
            return [Detection("person", (0, 0, 10, 10), 1.0)]
        raise RuntimeError("detector crashed on this frame")


def test_detector_failure_is_not_absence():
    pipeline = VisionPipeline(
        Frames(), [FlakyPersonDetector()], absent_frames_for_left=3
    )
    events = []
    for _ in range(4):
        events.extend(pipeline.step())
    assert PERSON_LEFT not in [e.kind for e in events]


def test_motion_failure_does_not_hide_successful_person_absence():
    class PersonLeaves:
        def __init__(self):
            self.n = 0

        def detect(self, frame):
            self.n += 1
            if self.n == 1:
                return [Detection("person", (0, 0, 10, 10), 1.0)]
            return []

    class BrokenMotionDetector(MotionDetector):
        def detect(self, frame):
            raise RuntimeError("motion processing failed")

    pipeline = VisionPipeline(
        Frames(), [PersonLeaves(), BrokenMotionDetector()], absent_frames_for_left=3
    )
    events = [event for _ in range(4) for event in pipeline.step()]
    assert [event.kind for event in events] == [PERSON_APPEARED, PERSON_LEFT]


def test_person_failure_breaks_consecutive_absence_streak():
    class InterruptedAbsence:
        def __init__(self):
            self.n = 0

        def detect(self, frame):
            self.n += 1
            if self.n == 1:
                return [Detection("person", (0, 0, 10, 10), 1.0)]
            if self.n == 4:
                raise RuntimeError("person detector unavailable")
            return []

    pipeline = VisionPipeline(
        Frames(), [InterruptedAbsence()], absent_frames_for_left=3
    )
    events = [event for _ in range(5) for event in pipeline.step()]
    assert [event.kind for event in events] == [PERSON_APPEARED]
    assert pipeline.step() == []
    assert [event.kind for event in pipeline.step()] == [PERSON_LEFT]
