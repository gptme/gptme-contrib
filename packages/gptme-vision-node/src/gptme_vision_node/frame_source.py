"""Frame sources: anything that can hand the pipeline a BGR frame.

All frames are numpy arrays in OpenCV's BGR channel order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}


@runtime_checkable
class FrameSource(Protocol):
    """A source of BGR frames."""

    def get_frame(self) -> np.ndarray | None:
        """Return the next frame (BGR), or None if none is available."""
        ...


class ImageFileSource:
    """Serve frames from a single image file or a directory of images.

    A single file is returned on every call. A directory is served
    round-robin in sorted filename order (wrapping unless ``loop=False``,
    in which case ``get_frame`` returns None after one full pass).
    """

    def __init__(self, path: str | Path, *, loop: bool = True) -> None:
        self.path = Path(path)
        self.loop = loop
        self._index = 0
        if self.path.is_dir():
            self._files = sorted(
                p for p in self.path.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS
            )
            if not self._files:
                raise ValueError(f"no images found in directory: {self.path}")
        elif self.path.is_file():
            self._files = [self.path]
        else:
            raise FileNotFoundError(f"no such file or directory: {self.path}")

    def get_frame(self) -> np.ndarray | None:
        if self._index >= len(self._files):
            if not self.loop:
                return None
            self._index = 0
        file = self._files[self._index]
        self._index += 1
        frame = cv2.imread(str(file), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError(f"failed to decode image: {file}")
        return frame

    def release(self) -> None:  # symmetry with OpenCVCameraSource
        pass


class OpenCVCameraSource:
    """Webcam (V4L2 index), Pi camera, or network stream (RTSP/HTTP URL).

    The capture is opened lazily on first ``get_frame`` so constructing the
    source is cheap and testable without hardware. After ``read_attempts``
    consecutive failed reads the capture is released and reopened on the
    next call, so a node recovers when a stream comes back.

    A source that has never opened raises ``RuntimeError`` naming it (typo,
    missing camera). Once it has opened, a failed reopen returns None;
    callers control the polling interval.
    """

    stop_on_empty = False

    def __init__(self, source: int | str, *, read_attempts: int = 3) -> None:
        if read_attempts < 1:
            raise ValueError("read_attempts must be at least 1")
        self.source = source
        self.read_attempts = read_attempts
        self._cap: cv2.VideoCapture | None = None
        self._has_opened = False

    def _ensure_open(self) -> cv2.VideoCapture | None:
        if self._cap is None:
            self._cap = cv2.VideoCapture(self.source)
            if not self._cap.isOpened():
                cap, self._cap = self._cap, None
                cap.release()
                if not self._has_opened:
                    raise RuntimeError(
                        f"failed to open capture source: {self.source!r}"
                    )
                return None
            self._has_opened = True
        return self._cap

    def get_frame(self) -> np.ndarray | None:
        cap = self._ensure_open()
        if cap is None:
            return None
        for _ in range(self.read_attempts):
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame
        # A dropped network stream (or unplugged webcam) never recovers on
        # the same handle; release it so the next call reopens the source.
        self.release()
        return None

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
