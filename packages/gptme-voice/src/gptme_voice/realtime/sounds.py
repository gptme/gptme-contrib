"""Pre-generated μ-law audio cues for Twilio transport sound effects.

Tones are generated at module load (pure stdlib, no audio deps) and
cached as bytes ready to pass directly to Twilio's media stream.
"""

import audioop
import math
import struct

_SAMPLE_RATE = 8000  # Twilio μ-law rate
_AMPLITUDE = 0.45  # keep below clipping; μ-law compresses anyway


def _gen_pcm_tone(freq_hz: float, duration_ms: int) -> bytes:
    """Generate a sine wave as 16-bit signed little-endian PCM at 8 kHz.

    Applies a short fade-in/fade-out (10% of duration) to avoid clicks.
    """
    n = int(_SAMPLE_RATE * duration_ms / 1000)
    fade = max(1, n // 10)
    samples: list[int] = []
    for i in range(n):
        t = i / _SAMPLE_RATE
        v = _AMPLITUDE * math.sin(2 * math.pi * freq_hz * t)
        if i < fade:
            v *= i / fade
        elif i > n - fade:
            v *= (n - i) / fade
        samples.append(int(32767 * v))
    return struct.pack(f"<{n}h", *samples)


def _gen_pcm_silence(duration_ms: int) -> bytes:
    return bytes(int(_SAMPLE_RATE * duration_ms / 1000) * 2)


def _to_mulaw(pcm: bytes) -> bytes:
    return audioop.lin2ulaw(pcm, 2)


# Dispatch cue: two rising tones — "I'm on it"
# 80 ms @ 880 Hz · 30 ms silence · 80 ms @ 1100 Hz
DISPATCH_CUE_MULAW: bytes = _to_mulaw(
    _gen_pcm_tone(880, 80) + _gen_pcm_silence(30) + _gen_pcm_tone(1100, 80)
)

# Timeout cue: single descending tone — "that didn't work"
# 200 ms @ 450 Hz
TIMEOUT_CUE_MULAW: bytes = _to_mulaw(_gen_pcm_tone(450, 200))
