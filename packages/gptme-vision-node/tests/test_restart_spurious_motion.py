"""pipeline.start() resets presence state and the stale frame but not the
MotionDetector background, so the first frame of every new session is diffed
against a frame from the previous session and fires a spurious motion event."""

from __future__ import annotations

import numpy as np
from gptme_vision_node.detect import MotionDetector
from gptme_vision_node.pipeline import MOTION, VisionPipeline


class ListSource:
    def __init__(self, frames):
        self.frames = list(frames)

    def get_frame(self):
        return self.frames.pop(0) if self.frames else None


def _run_to_exhaustion(p: VisionPipeline) -> None:
    p.start()
    t = p._thread
    assert t is not None
    t.join(2)
    assert not t.is_alive()


def test_restart_does_not_fire_motion_on_first_frame():
    dark = np.full((120, 160, 3), 20, dtype=np.uint8)
    lit = np.full((120, 160, 3), 220, dtype=np.uint8)  # lights on while disconnected
    events = []
    p = VisionPipeline(
        ListSource([dark, dark]),
        [MotionDetector()],
        interval_s=0.001,
        on_event=events.append,
    )
    _run_to_exhaustion(p)
    assert events == []

    # Reconnect: new session, a single still frame of the (changed) scene.
    p.source = ListSource([lit])
    _run_to_exhaustion(p)
    assert (
        [e.kind for e in events if e.kind == MOTION] == []
    ), "first frame after restart was diffed against the previous session's background"

    # Control: a fresh pipeline primes on its first frame and reports nothing.
    fresh = []
    q = VisionPipeline(
        ListSource([lit]), [MotionDetector()], interval_s=0.001, on_event=fresh.append
    )
    _run_to_exhaustion(q)
    assert fresh == []
