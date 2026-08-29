"""Shared synthetic-image helpers for the vision-node tests."""

from __future__ import annotations

import numpy as np
import pytest


def solid_frame(
    color: tuple[int, int, int], size: tuple[int, int] = (240, 320)
) -> np.ndarray:
    """A solid BGR frame."""
    frame = np.zeros((*size, 3), dtype=np.uint8)
    frame[:] = color
    return frame


def scene_frame(seed: int, size: tuple[int, int] = (240, 320)) -> np.ndarray:
    """A deterministic 'room': colored background + a few rectangles."""
    rng = np.random.default_rng(seed)
    frame = solid_frame(tuple(int(c) for c in rng.integers(0, 256, 3)), size)
    h, w = size
    for _ in range(4):
        x0, y0 = int(rng.integers(0, w // 2)), int(rng.integers(0, h // 2))
        x1, y1 = x0 + int(rng.integers(20, w // 2)), y0 + int(rng.integers(20, h // 2))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        frame[y0:y1, x0:x1] = color
    return frame


@pytest.fixture
def gray_frame() -> np.ndarray:
    return solid_frame((128, 128, 128))
