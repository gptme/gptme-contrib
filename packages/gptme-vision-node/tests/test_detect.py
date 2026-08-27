"""Tests for person/motion detection on synthetic frames."""

from __future__ import annotations

import numpy as np
from gptme_vision_node.detect import Detection, MotionDetector, PersonDetector

from .conftest import scene_frame, solid_frame


def test_motion_detector_first_frame_primes(gray_frame):
    detector = MotionDetector()
    assert detector.detect(gray_frame) == []


def test_motion_detector_fires_on_change(gray_frame):
    detector = MotionDetector()
    detector.detect(gray_frame)  # prime background

    moved = gray_frame.copy()
    moved[80:160, 100:220] = (255, 255, 255)  # a bright object appears
    detections = detector.detect(moved)

    assert detections, "expected motion detections on a large change"
    assert all(d.kind == "motion" for d in detections)
    x, y, w, h = detections[0].box
    assert w > 0 and h > 0
    assert 0.0 < detections[0].score <= 1.0


def test_motion_detector_quiet_on_static_scene(gray_frame):
    detector = MotionDetector()
    detector.detect(gray_frame)
    for _ in range(3):
        assert detector.detect(gray_frame.copy()) == []


def test_person_detector_no_crash_on_synthetic(gray_frame):
    """HOG must run without error; fake scenes may legitimately yield nothing."""
    detector = PersonDetector()
    for frame in (gray_frame, scene_frame(1), scene_frame(2)):
        detections = detector.detect(frame)
        assert isinstance(detections, list)
        for d in detections:
            assert isinstance(d, Detection)
            assert d.kind == "person"


def test_person_detector_handles_tiny_frame():
    tiny = solid_frame((50, 50, 50), size=(32, 32))
    assert isinstance(PersonDetector().detect(tiny), list)


def test_detection_dataclass():
    d = Detection(kind="person", box=(1, 2, 3, 4), score=0.5)
    assert (d.kind, d.box, d.score) == ("person", (1, 2, 3, 4), 0.5)
    assert d == Detection(kind="person", box=(1, 2, 3, 4), score=0.5)
    assert isinstance(np.asarray(d.box), np.ndarray)  # plain tuple, numpy-friendly
