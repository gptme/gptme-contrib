"""CLI for the vision node.

Subcommands:

- ``detect``   — run detectors on a source, print detections
- ``look``     — describe the current frame via a vision LLM
- ``enroll``   — add sample(s) of a named place to the gallery
- ``whereami`` — rank enrolled places for the current observation
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
import numpy as np

from .detect import Detector, MotionDetector, PersonDetector
from .frame_source import FrameSource, ImageFileSource, OpenCVCameraSource
from .look import describe_frame
from .place import PlaceRecognizer, WifiSignature

DEFAULT_GALLERY = Path.home() / ".config" / "gptme-vision-node" / "places.json"

source_option = click.option(
    "--source",
    "source_spec",
    required=True,
    help="image file/dir, camera:N, or a stream URL (rtsp://...)",
)


def make_source(spec: str) -> FrameSource:
    """Parse a source spec: ``camera:N``, a stream URL, or an image path."""
    try:
        if spec.startswith("camera:"):
            return OpenCVCameraSource(int(spec.split(":", 1)[1]))
        if "://" in spec:  # rtsp://, http://, ...
            return OpenCVCameraSource(spec)
        return ImageFileSource(spec)
    except ValueError as exc:
        if spec.startswith("camera:"):
            raise click.ClickException(f"invalid camera index: {spec!r}") from exc
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc


def _get_frame_or_die(source: FrameSource) -> np.ndarray:
    try:
        frame = source.get_frame()
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if frame is None:
        raise click.ClickException("source produced no frame")
    return frame


@click.group()
def main() -> None:
    """Vision pipeline for the BobBrain presence node."""


@main.command()
@source_option
@click.option("--once", is_flag=True, help="single frame, then exit")
@click.option("--interval", type=float, default=1.0, help="seconds between frames")
def detect(source_spec: str, once: bool, interval: float) -> None:
    """Run person/motion detection on a source."""
    source = make_source(source_spec)
    detectors: list[Detector] = [PersonDetector(), MotionDetector()]
    try:
        while True:
            frame = _get_frame_or_die(source)
            try:
                detections = [d for det in detectors for d in det.detect(frame)]
            except (RuntimeError, ValueError) as exc:
                raise click.ClickException(str(exc)) from exc
            for d in detections:
                click.echo(f"{d.kind}\tbox={d.box}\tscore={d.score:.3f}")
            if not detections:
                click.echo("(no detections)")
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        release = getattr(source, "release", None)
        if release:
            release()


@main.command()
@source_option
@click.option("--prompt", default="Describe what you see, briefly.")
@click.option("--model", default=None, help="override VISION_MODEL")
def look(source_spec: str, prompt: str, model: str | None) -> None:
    """Describe the current frame via a vision LLM (needs OPENAI_API_KEY)."""
    frame = _get_frame_or_die(make_source(source_spec))
    click.echo(describe_frame(frame, prompt, model=model))


@main.command()
@source_option
@click.option("--place", "place_name", required=True, help="place name")
@click.option("--gallery", default=str(DEFAULT_GALLERY), show_default=True)
@click.option("--no-wifi", is_flag=True, help="skip the wifi scan")
def enroll(source_spec: str, place_name: str, gallery: str, no_wifi: bool) -> None:
    """Enroll a sample of a named place into the gallery."""
    gallery_path = Path(gallery)
    recognizer = (
        PlaceRecognizer.from_file(gallery_path)
        if gallery_path.exists()
        else PlaceRecognizer()
    )
    frame = _get_frame_or_die(make_source(source_spec))
    wifi = {} if no_wifi else WifiSignature.scan()
    recognizer.enroll(place_name, frame, wifi_sig=wifi or None)
    recognizer.save(gallery_path)
    n = len(recognizer.places[place_name])
    click.echo(
        f"enrolled '{place_name}' (sample {n}, wifi APs: {len(wifi)}) -> {gallery_path}"
    )


@main.command()
@source_option
@click.option("--gallery", default=str(DEFAULT_GALLERY), show_default=True)
@click.option("--no-wifi", is_flag=True, help="skip the wifi scan")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
def whereami(source_spec: str, gallery: str, no_wifi: bool, as_json: bool) -> None:
    """Rank enrolled places for the current view."""
    gallery_path = Path(gallery)
    if not gallery_path.exists():
        raise click.ClickException(
            f"no gallery at {gallery_path} (enroll some places first)"
        )
    recognizer = PlaceRecognizer.from_file(gallery_path)
    frame = _get_frame_or_die(make_source(source_spec))
    wifi = {} if no_wifi else WifiSignature.scan()
    results = recognizer.recognize(frame, wifi_sig=wifi or None)
    if not results:
        raise click.ClickException(
            "no matches (gallery empty or no comparable signals)"
        )
    if as_json:
        click.echo(json.dumps([{"place": n, "score": s} for n, s in results]))
    else:
        for name, score in results:
            click.echo(f"{score:.3f}\t{name}")


if __name__ == "__main__":
    main()
