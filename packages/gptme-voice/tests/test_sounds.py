"""Level and structure regression tests for the voice audio cues.

Guards against cue gain creeping back to a level that dominates speech: the
original 0.45 amplitude was reported as "very loud" on a real call
(Erik, 2026-08-31). ``sounds.py`` carries the enforced peak/RMS ceilings.
"""

import audioop
import math
import struct

from gptme_voice.realtime import sounds

SAMPLE_RATE = sounds.SAMPLE_RATE

# Cues must stay above this floor so a future "fix" cannot silence them.
_MIN_AUDIBLE_RMS = 0.02


def _peak(pcm: bytes) -> float:
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return max(abs(s) for s in samples) / 32767.0


def _rms(pcm: bytes) -> float:
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32767.0


def _speech_reference_pcm(
    peak: float = 0.7, duration_s: float = 1.0, f0: float = 120.0
) -> bytes:
    """Deterministic broadband voiced-speech proxy at a representative TTS level.

    A harmonic stack under a formant envelope with 4 Hz syllabic amplitude
    modulation. Not a recording, but a stable stand-in for the speech the cue
    shares the transport with. Peak 0.7 ≈ a loud TTS turn, which is the
    loudest speech the cue is expected to sit under.
    """
    n = int(SAMPLE_RATE * duration_s)
    formants = ((700.0, 1.0, 120.0), (1220.0, 0.6, 150.0), (2600.0, 0.35, 200.0))
    raw: list[float] = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 0.6 + 0.4 * math.sin(2 * math.pi * 4.0 * t)
        value = 0.0
        for k in range(1, 41):
            freq = f0 * k
            if freq >= SAMPLE_RATE / 2:
                break
            gain = sum(g / (1 + ((freq - fc) / bw) ** 2) for fc, g, bw in formants)
            value += gain * math.sin(2 * math.pi * freq * t)
        raw.append(value * env)
    norm = max(abs(x) for x in raw)
    return struct.pack(f"<{n}h", *[int(32767 * peak * x / norm) for x in raw])


class TestDispatchCueLevel:
    def test_pcm_peak_and_rms_within_ceiling(self) -> None:
        assert _peak(sounds.DISPATCH_CUE_PCM) <= sounds.DISPATCH_CUE_MAX_PEAK
        assert _rms(sounds.DISPATCH_CUE_PCM) <= sounds.DISPATCH_CUE_MAX_RMS

    def test_mulaw_transport_within_ceiling(self) -> None:
        decoded = audioop.ulaw2lin(sounds.DISPATCH_CUE_MULAW, 2)
        assert _peak(decoded) <= sounds.DISPATCH_CUE_MAX_PEAK
        assert _rms(decoded) <= sounds.DISPATCH_CUE_MAX_RMS

    def test_quieter_than_speech_reference(self) -> None:
        speech_rms = _rms(_speech_reference_pcm())
        assert _rms(sounds.DISPATCH_CUE_PCM) < speech_rms

    def test_remains_audible(self) -> None:
        assert _rms(sounds.DISPATCH_CUE_PCM) >= _MIN_AUDIBLE_RMS

    def test_two_tone_structure_preserved(self) -> None:
        """The cue is still the rising two-tone blip, not silence or noise."""
        pcm = sounds.DISPATCH_CUE_PCM
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        mid = len(samples) // 2
        # A ~30 ms silent gap separates the two 80 ms tones near the midpoint.
        gap = samples[mid - 100 : mid + 100]
        assert max(abs(s) for s in gap) < _peak(pcm) * 32767 * 0.35


class TestTimeoutCueLevel:
    def test_pcm_peak_and_rms_within_ceiling(self) -> None:
        assert _peak(sounds.TIMEOUT_CUE_PCM) <= sounds.TIMEOUT_CUE_MAX_PEAK
        assert _rms(sounds.TIMEOUT_CUE_PCM) <= sounds.TIMEOUT_CUE_MAX_RMS

    def test_mulaw_transport_within_ceiling(self) -> None:
        decoded = audioop.ulaw2lin(sounds.TIMEOUT_CUE_MULAW, 2)
        assert _peak(decoded) <= sounds.TIMEOUT_CUE_MAX_PEAK
        assert _rms(decoded) <= sounds.TIMEOUT_CUE_MAX_RMS

    def test_quieter_than_speech_reference(self) -> None:
        speech_rms = _rms(_speech_reference_pcm())
        assert _rms(sounds.TIMEOUT_CUE_PCM) < speech_rms

    def test_remains_audible(self) -> None:
        assert _rms(sounds.TIMEOUT_CUE_PCM) >= _MIN_AUDIBLE_RMS


def test_pcm_cues_mapping_matches_named_cues() -> None:
    assert sounds.PCM_CUES["dispatch"] == sounds.DISPATCH_CUE_PCM
    assert sounds.PCM_CUES["timeout"] == sounds.TIMEOUT_CUE_PCM
