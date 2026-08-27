"""Tests for the CLI (offline; sources are image files, LLM/wifi mocked)."""

from __future__ import annotations

import json

import cv2
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
