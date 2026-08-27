"""CLI for the vision node.

Subcommands:

- ``detect``   — run detectors on a source, print detections
- ``look``     — describe the current frame via a vision LLM
- ``enroll``   — add sample(s) of a named place to the gallery
- ``whereami`` — rank enrolled places for the current observation
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .detect import Detector, MotionDetector, PersonDetector
from .frame_source import FrameSource, ImageFileSource, OpenCVCameraSource
from .look import describe_frame
from .place import PlaceRecognizer, WifiSignature

DEFAULT_GALLERY = Path.home() / ".config" / "gptme-vision-node" / "places.json"


def make_source(spec: str) -> FrameSource:
    """Parse a source spec: ``camera:N``, a stream URL, or an image path."""
    if spec.startswith("camera:"):
        return OpenCVCameraSource(int(spec.split(":", 1)[1]))
    if "://" in spec:  # rtsp://, http://, ...
        return OpenCVCameraSource(spec)
    return ImageFileSource(spec)


def _get_frame_or_die(source: FrameSource):
    frame = source.get_frame()
    if frame is None:
        print("error: source produced no frame", file=sys.stderr)
        raise SystemExit(1)
    return frame


def cmd_detect(args: argparse.Namespace) -> int:
    source = make_source(args.source)
    detectors: list[Detector] = [PersonDetector(), MotionDetector()]
    try:
        while True:
            frame = source.get_frame()
            if frame is None:
                break
            detections = [d for det in detectors for d in det.detect(frame)]
            for d in detections:
                print(f"{d.kind}\tbox={d.box}\tscore={d.score:.3f}")
            if not detections:
                print("(no detections)")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        release = getattr(source, "release", None)
        if release:
            release()
    return 0


def cmd_look(args: argparse.Namespace) -> int:
    source = make_source(args.source)
    frame = _get_frame_or_die(source)
    print(describe_frame(frame, args.prompt, model=args.model))
    return 0


def _load_recognizer(gallery: Path) -> PlaceRecognizer:
    if gallery.exists():
        return PlaceRecognizer.from_file(gallery)
    return PlaceRecognizer()


def cmd_enroll(args: argparse.Namespace) -> int:
    gallery = Path(args.gallery)
    recognizer = _load_recognizer(gallery)
    source = make_source(args.source)
    frame = _get_frame_or_die(source)
    wifi = {} if args.no_wifi else WifiSignature.scan()
    recognizer.enroll(args.place, frame, wifi_sig=wifi or None)
    recognizer.save(gallery)
    n = len(recognizer.places[args.place])
    print(
        f"enrolled '{args.place}' (sample {n}, " f"wifi APs: {len(wifi)}) -> {gallery}"
    )
    return 0


def cmd_whereami(args: argparse.Namespace) -> int:
    gallery = Path(args.gallery)
    if not gallery.exists():
        print(
            f"error: no gallery at {gallery} (enroll some places first)",
            file=sys.stderr,
        )
        return 1
    recognizer = PlaceRecognizer.from_file(gallery)
    source = make_source(args.source)
    frame = _get_frame_or_die(source)
    wifi = {} if args.no_wifi else WifiSignature.scan()
    results = recognizer.recognize(frame, wifi_sig=wifi or None)
    if not results:
        print("no matches (gallery empty or no comparable signals)")
        return 1
    if args.json:
        print(json.dumps([{"place": n, "score": s} for n, s in results]))
    else:
        for name, score in results:
            print(f"{score:.3f}\t{name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gptme-vision-node",
        description="Vision pipeline for the BobBrain presence node",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--source",
            required=True,
            help="image file/dir, camera:N, or a stream URL (rtsp://...)",
        )

    p_detect = sub.add_parser("detect", help="run person/motion detection")
    add_source(p_detect)
    p_detect.add_argument("--once", action="store_true", help="single frame, then exit")
    p_detect.add_argument(
        "--interval", type=float, default=1.0, help="seconds between frames"
    )
    p_detect.set_defaults(func=cmd_detect)

    p_look = sub.add_parser("look", help="describe the current frame via a vision LLM")
    add_source(p_look)
    p_look.add_argument("--prompt", default="Describe what you see, briefly.")
    p_look.add_argument("--model", default=None, help="override VISION_MODEL")
    p_look.set_defaults(func=cmd_look)

    p_enroll = sub.add_parser("enroll", help="enroll a sample of a named place")
    add_source(p_enroll)
    p_enroll.add_argument("--place", required=True, help="place name")
    p_enroll.add_argument("--gallery", default=str(DEFAULT_GALLERY))
    p_enroll.add_argument("--no-wifi", action="store_true", help="skip the wifi scan")
    p_enroll.set_defaults(func=cmd_enroll)

    p_where = sub.add_parser(
        "whereami", help="rank enrolled places for the current view"
    )
    add_source(p_where)
    p_where.add_argument("--gallery", default=str(DEFAULT_GALLERY))
    p_where.add_argument("--no-wifi", action="store_true", help="skip the wifi scan")
    p_where.add_argument("--json", action="store_true", help="JSON output")
    p_where.set_defaults(func=cmd_whereami)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
