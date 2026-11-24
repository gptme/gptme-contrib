"""A transient PersonDetector exception is counted as 'nobody in frame',
so a person who never left gets a person_left event."""

from __future__ import annotations

import numpy as np
from gptme_vision_node.detect import Detection
from gptme_vision_node.pipeline import PERSON_LEFT, VisionPipeline


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
