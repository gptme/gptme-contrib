"""Person and motion detection with plain OpenCV — no model downloads.

``PersonDetector`` uses OpenCV's built-in HOG people detector.
``MotionDetector`` uses frame differencing against a running-average
background model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

Box = tuple[int, int, int, int]  # x, y, w, h


@dataclass(frozen=True)
class Detection:
    """One detection: what, where, and how confident."""

    kind: str  # "person" | "motion"
    box: Box
    score: float


@runtime_checkable
class Detector(Protocol):
    """Anything that can turn a frame into detections."""

    def detect(self, frame: np.ndarray) -> list[Detection]: ...


class PersonDetector:
    """HOG + linear-SVM people detector (ships with OpenCV, no downloads).

    Not state of the art, but dependency-free and fine for "someone is
    here" presence events at v0. Swap for a real model later via the same
    ``detect(frame) -> list[Detection]`` interface.
    """

    def __init__(
        self,
        *,
        win_stride: tuple[int, int] = (8, 8),
        scale: float = 1.05,
        score_threshold: float = 0.0,
    ) -> None:
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(
            np.asarray(cv2.HOGDescriptor.getDefaultPeopleDetector(), dtype=np.float64)
        )
        self.win_stride = win_stride
        self.scale = scale
        self.score_threshold = score_threshold

    def detect(self, frame: np.ndarray) -> list[Detection]:
        # HOG needs a minimum window (64x128); upscale tiny frames and map
        # detections back to the caller's coordinate space.
        original_h, original_w = frame.shape[:2]
        input_h, input_w = max(original_h, 128), max(original_w, 64)
        if (input_h, input_w) != (original_h, original_w):
            frame = cv2.resize(frame, (input_w, input_h))
        boxes, weights = self._hog.detectMultiScale(
            frame, winStride=self.win_stride, scale=self.scale
        )
        x_scale = original_w / input_w
        y_scale = original_h / input_h
        detections = []
        for box, weight in zip(boxes, np.ravel(weights)):
            score = float(weight)
            if score < self.score_threshold:
                continue
            x, y, bw, bh = (int(v) for v in box)
            mapped_box = (
                round(x * x_scale),
                round(y * y_scale),
                round(bw * x_scale),
                round(bh * y_scale),
            )
            detections.append(Detection(kind="person", box=mapped_box, score=score))
        return detections


class MotionDetector:
    """Frame differencing against an exponentially averaged background.

    The first frame primes the background and yields no detections.
    Subsequent frames are diffed against the background; contiguous
    changed regions above ``min_area`` become ``motion`` detections whose
    score is the fraction of the frame that changed (capped at 1.0).
    """

    def __init__(
        self,
        *,
        alpha: float = 0.1,
        threshold: int = 25,
        min_area_fraction: float = 0.005,
        blur_ksize: int = 5,
    ) -> None:
        if blur_ksize <= 0 or blur_ksize % 2 == 0:
            raise ValueError("blur_ksize must be a positive odd integer")
        self.alpha = alpha
        self.threshold = threshold
        self.min_area_fraction = min_area_fraction
        self.blur_ksize = blur_ksize
        self._background: np.ndarray | None = None

    def reset(self) -> None:
        self._background = None

    def _prepare(self, frame: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.blur_ksize > 1:
            gray = cv2.GaussianBlur(gray, (self.blur_ksize, self.blur_ksize), 0)
        return gray.astype(np.float32)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        gray = self._prepare(frame)
        if self._background is None or self._background.shape != gray.shape:
            self._background = gray
            return []

        diff = cv2.absdiff(gray, self._background)
        # Update the background after diffing so sustained change keeps firing
        # for a while, then fades in as the new background.
        cv2.accumulateWeighted(gray, self._background, self.alpha)

        mask = (diff > self.threshold).astype(np.uint8) * 255
        mask = np.asarray(
            cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2), dtype=np.uint8
        )

        frame_area = mask.shape[0] * mask.shape[1]
        min_area = self.min_area_fraction * frame_area
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        detections = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            score = min(1.0, float(area) / frame_area * 10.0)
            detections.append(Detection(kind="motion", box=(x, y, w, h), score=score))
        return detections
