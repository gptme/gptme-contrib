"""Tests for the CLI (offline; sources are image files, LLM/wifi mocked)."""

from __future__ import annotations

import json

import cv2
import httpx
import pytest
from click.testing import CliRunner
from gptme_vision_node import cli
from gptme_vision_node.frame_source import ImageFileSource, OpenCVCameraSource

from .conftest import scene_frame


def _write_scene(path, seed):
    assert cv2.imwrite(str(path), scene_frame(seed))
    return path


def test_make_source_parsing(tmp_path):
    img = _write_scene(tmp_path / "a.png", 1)
    assert isinstance(cli.make_source(str(img)), ImageFileSource)
    cam = cli.make_source("camera:2")
    assert isinstance(cam, OpenCVCameraSource) and cam.source == 2
    rtsp = cli.make_source("rtsp://host/stream")
    assert isinstance(rtsp, OpenCVCameraSource) and rtsp.source == "rtsp://host/stream"


def test_cli_invalid_camera_index_is_clean_error():
    result = CliRunner().invoke(cli.main, ["detect", "--source", "camera:nope"])
    assert result.exit_code != 0
    assert "invalid camera index" in result.output
    assert "Traceback" not in result.output


def test_cli_capture_failure_is_clean_error(monkeypatch):
    class BrokenSource:
        def get_frame(self):
            raise RuntimeError("camera unavailable")

    monkeypatch.setattr(cli, "make_source", lambda spec: BrokenSource())
    result = CliRunner().invoke(cli.main, ["look", "--source", "camera:9"])
    assert result.exit_code != 0
    assert "camera unavailable" in result.output
    assert "Traceback" not in result.output


def test_capture_frame_releases_source(monkeypatch):
    class TrackingSource:
        released = False

        def get_frame(self):
            return scene_frame(4)

        def release(self):
            self.released = True

    source = TrackingSource()
    monkeypatch.setattr(cli, "make_source", lambda spec: source)

    assert cli._capture_frame("camera:4").shape == scene_frame(4).shape
    assert source.released


def test_capture_frame_releases_source_after_failure(monkeypatch):
    class BrokenSource:
        released = False

        def get_frame(self):
            raise RuntimeError("camera unavailable")

        def release(self):
            self.released = True

    source = BrokenSource()
    monkeypatch.setattr(cli, "make_source", lambda spec: source)

    with pytest.raises(cli.click.ClickException, match="camera unavailable"):
        cli._capture_frame("camera:9")
    assert source.released


def test_cli_look_endpoint_failure_is_clean_error(tmp_path, monkeypatch):
    img = _write_scene(tmp_path / "a.png", 5)

    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("vision endpoint timed out")

    monkeypatch.setattr(cli, "describe_frame", timeout)
    result = CliRunner().invoke(cli.main, ["look", "--source", str(img)])

    assert result.exit_code != 0
    assert "vision endpoint timed out" in result.output
    assert "Traceback" not in result.output


def test_cli_detect_once(tmp_path):
    img = _write_scene(tmp_path / "a.png", 2)
    result = CliRunner().invoke(cli.main, ["detect", "--source", str(img), "--once"])
    assert result.exit_code == 0
    assert result.output.strip()  # printed detections or "(no detections)"


def test_cli_enroll_and_whereami(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli.WifiSignature, "scan", staticmethod(lambda interface=None: {})
    )
    runner = CliRunner()
    gallery = tmp_path / "places.json"
    kitchen = _write_scene(tmp_path / "kitchen.png", 101)
    office = _write_scene(tmp_path / "office.png", 202)

    for place, img in (("kitchen", kitchen), ("office", office)):
        result = runner.invoke(
            cli.main,
            [
                "enroll",
                "--place",
                place,
                "--source",
                str(img),
                "--gallery",
                str(gallery),
            ],
        )
        assert result.exit_code == 0, result.output
    assert gallery.exists()

    result = runner.invoke(
        cli.main,
        ["whereami", "--source", str(kitchen), "--gallery", str(gallery), "--json"],
    )
    assert result.exit_code == 0, result.output
    results = json.loads(result.output.splitlines()[-1])
    assert results[0]["place"] == "kitchen"
    assert results[0]["score"] > results[-1]["score"]


def test_cli_whereami_missing_gallery(tmp_path):
    img = _write_scene(tmp_path / "a.png", 3)
    result = CliRunner().invoke(
        cli.main,
        ["whereami", "--source", str(img), "--gallery", str(tmp_path / "none.json")],
    )
    assert result.exit_code != 0
    assert "no gallery" in result.output
