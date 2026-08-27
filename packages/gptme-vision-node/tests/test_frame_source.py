"""Tests for frame sources (offline: temp image files only)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from gptme_vision_node.frame_source import FrameSource, ImageFileSource

from .conftest import solid_frame


def _write(path, color):
    assert cv2.imwrite(str(path), solid_frame(color))


def test_single_file_source_repeats(tmp_path):
    img = tmp_path / "a.png"
    _write(img, (255, 0, 0))
    source = ImageFileSource(img)
    for _ in range(3):
        frame = source.get_frame()
        assert frame is not None
        assert frame.shape == (240, 320, 3)
        assert (frame[0, 0] == np.array([255, 0, 0])).all()


def test_directory_round_robin(tmp_path):
    _write(tmp_path / "a.png", (255, 0, 0))
    _write(tmp_path / "b.png", (0, 255, 0))
    _write(tmp_path / "c.png", (0, 0, 255))
    source = ImageFileSource(tmp_path)
    colors = [tuple(source.get_frame()[0, 0]) for _ in range(6)]
    expected = [(255, 0, 0), (0, 255, 0), (0, 0, 255)] * 2  # sorted order, wraps
    assert colors == expected


def test_directory_no_loop_exhausts(tmp_path):
    _write(tmp_path / "a.png", (10, 10, 10))
    source = ImageFileSource(tmp_path, loop=False)
    assert source.get_frame() is not None
    assert source.get_frame() is None


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ImageFileSource(tmp_path / "nope.png")


def test_empty_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        ImageFileSource(tmp_path)


def test_satisfies_protocol(tmp_path):
    _write(tmp_path / "a.png", (1, 2, 3))
    assert isinstance(ImageFileSource(tmp_path), FrameSource)
