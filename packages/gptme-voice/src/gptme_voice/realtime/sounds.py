"""Pre-generated audio cues for voice transports.

Tones are generated at module load and cached as PCM16 for local/browser
clients and μ-law for Twilio's media stream.
"""

import audioop
import math
import struct

SAMPLE_RATE = 8000

# Cue amplitudes are deliberately well below full scale. On the Twilio μ-law
# path a pure tone sits in a single critical band and reads perceptually louder
# than speech at the same level, so a hot tone dominates the conversation
# instead of acting as a subtle progress signal. The original 0.45 amplitude
# was reported as "very loud" on a real call (2026-08-31), where it peaked
# at −6.9 dBFS with −11.3 dBFS RMS — hotter than normal speech. Both cues now
# sit below the RMS of a representative speech reference; see
# ``tests/test_sounds.py`` for the enforced peak/RMS ceilings.
_DISPATCH_AMPLITUDE = 0.12
_TIMEOUT_AMPLITUDE = 0.15

# Enforced level contract (see tests/test_sounds.py). Peak/RMS are measured on
# the decoded 16-bit PCM, normalized to full scale (1.0).
DISPATCH_CUE_MAX_PEAK = 0.15
DISPATCH_CUE_MAX_RMS = 0.09
TIMEOUT_CUE_MAX_PEAK = 0.20
TIMEOUT_CUE_MAX_RMS = 0.13


def _gen_pcm_tone(freq_hz: float, duration_ms: int, amplitude: float) -> bytes:
    """Generate a sine wave as 16-bit signed little-endian PCM at 8 kHz.

    Applies a short fade-in/fade-out (10% of duration) to avoid clicks.
    """
    n = int(SAMPLE_RATE * duration_ms / 1000)
    fade = max(1, n // 10)
    samples: list[int] = []
    for i in range(n):
        t = i / SAMPLE_RATE
        v = amplitude * math.sin(2 * math.pi * freq_hz * t)
        if i < fade:
            v *= i / fade
        elif i > n - fade:
            v *= (n - i) / fade
        samples.append(int(32767 * v))
    return struct.pack(f"<{n}h", *samples)


def _gen_pcm_silence(duration_ms: int) -> bytes:
    return bytes(int(SAMPLE_RATE * duration_ms / 1000) * 2)


def _to_mulaw(pcm: bytes) -> bytes:
    return audioop.lin2ulaw(pcm, 2)


# Dispatch cue: two rising tones — "I'm on it"
# 80 ms @ 880 Hz · 30 ms silence · 80 ms @ 1100 Hz
DISPATCH_CUE_PCM: bytes = (
    _gen_pcm_tone(880, 80, _DISPATCH_AMPLITUDE)
    + _gen_pcm_silence(30)
    + _gen_pcm_tone(1100, 80, _DISPATCH_AMPLITUDE)
)

# Timeout cue: single low tone — "that didn't work"
# 200 ms @ 450 Hz. Kept slightly louder than the dispatch cue (it marks a
# failure), but still bounded below the speech reference.
TIMEOUT_CUE_PCM: bytes = _gen_pcm_tone(450, 200, _TIMEOUT_AMPLITUDE)

DISPATCH_CUE_MULAW: bytes = _to_mulaw(DISPATCH_CUE_PCM)
TIMEOUT_CUE_MULAW: bytes = _to_mulaw(TIMEOUT_CUE_PCM)

PCM_CUES: dict[str, bytes] = {
    "dispatch": DISPATCH_CUE_PCM,
    "timeout": TIMEOUT_CUE_PCM,
}
