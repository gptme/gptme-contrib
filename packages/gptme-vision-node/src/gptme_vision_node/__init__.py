"""Vision pipeline for the BobBrain presence node.

Frame capture, person/motion detection, an LLM "look" tool, and
place recognition (visual + WiFi fingerprints). See the BobBrain spec
(ErikBjare/bob#730) — this is milestone 2 (vision v0).
"""

from .bridge import VisionBridge
from .detect import Detection, Detector, MotionDetector, PersonDetector
from .frame_source import FrameSource, ImageFileSource, OpenCVCameraSource
from .look import describe_frame
from .pipeline import VisionEvent, VisionPipeline
from .place import Embedder, HistogramEmbedder, PlaceRecognizer, WifiSignature

__all__ = [
    "Detection",
    "Detector",
    "Embedder",
    "FrameSource",
    "HistogramEmbedder",
    "ImageFileSource",
    "MotionDetector",
    "OpenCVCameraSource",
    "PersonDetector",
    "PlaceRecognizer",
    "VisionBridge",
    "VisionEvent",
    "VisionPipeline",
    "WifiSignature",
    "describe_frame",
]

__version__ = "0.1.0"
