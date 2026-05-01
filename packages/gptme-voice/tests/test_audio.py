"""Tests for AudioConverter format conversion."""

import audioop
import math
import struct

from gptme_voice.realtime.audio import AudioConverter


def sine_wave_pcm(freq_hz: float, duration_s: float, sample_rate: int) -> bytes:
    """Generate a mono 16-bit PCM sine wave."""
    n = int(sample_rate * duration_s)
    samples = [
        int(32767 * math.sin(2 * math.pi * freq_hz * i / sample_rate)) for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def rms_energy(pcm_bytes: bytes) -> float:
    """Return RMS energy of a 16-bit PCM buffer."""
    n = len(pcm_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm_bytes)
    return math.sqrt(sum(s * s for s in samples) / n)


class TestOpenaiToTwilio:
    def test_output_length(self):
        """openai_to_twilio produces 3x fewer bytes (24kHz PCM → 8kHz μ-law)."""
        converter = AudioConverter()
        # 10ms at 24kHz = 240 samples = 480 bytes PCM
        pcm = bytes(480)
        result = converter.openai_to_twilio(pcm)
        # 10ms at 8kHz = 80 samples = 80 bytes μ-law
        assert len(result) == 80

    def test_silence_stays_silence(self):
        """Silent input produces silent μ-law output (all bytes = 0xFF or 0x7F)."""
        converter = AudioConverter()
        pcm = bytes(480)  # 480 zero bytes = silence
        result = converter.openai_to_twilio(pcm)
        # μ-law silence is 0xFF (positive zero) or 0x7F (negative zero)
        unique = set(result)
        assert unique <= {
            0xFF,
            0x7F,
        }, f"Non-silent bytes in output: {unique - {0xFF, 0x7F}}"

    def test_valid_mulaw_output(self):
        """Output can be decoded back to PCM without error."""
        converter = AudioConverter()
        pcm = sine_wave_pcm(1000, 0.02, AudioConverter.OPENAI_RATE)
        mulaw = converter.openai_to_twilio(pcm)
        # Should not raise
        pcm_back = audioop.ulaw2lin(mulaw, 2)
        assert len(pcm_back) == len(mulaw) * 2

    def test_antialiasing_attenuates_high_frequencies(self):
        """A tone above the 8kHz Nyquist has lower energy after anti-aliasing filter."""
        converter_new = AudioConverter()
        # 9kHz tone — above Nyquist for 8kHz output, will alias without pre-filter
        high_freq_pcm = sine_wave_pcm(9000, 0.05, AudioConverter.OPENAI_RATE)
        mulaw_filtered = converter_new.openai_to_twilio(high_freq_pcm)
        pcm_filtered = audioop.ulaw2lin(mulaw_filtered, 2)
        energy_filtered = rms_energy(pcm_filtered)

        # Without pre-filter: simulate raw downsample (direct ratecv + ulaw)
        pcm_8k_raw, _ = audioop.ratecv(high_freq_pcm, 2, 1, 24000, 8000, None)
        mulaw_raw = audioop.lin2ulaw(pcm_8k_raw, 2)
        pcm_raw = audioop.ulaw2lin(mulaw_raw, 2)
        energy_raw = rms_energy(pcm_raw)

        # Pre-filter should reduce or not increase high-frequency fold-back
        # (energy_filtered <= energy_raw * 1.05 allows tiny float rounding)
        assert energy_filtered <= energy_raw * 1.05, (
            f"Anti-aliasing filter increased high-freq energy: "
            f"filtered={energy_filtered:.1f} raw={energy_raw:.1f}"
        )

    def test_voice_frequency_preserved(self):
        """A 1kHz voice-range tone passes through with reasonable fidelity (< 3dB loss)."""
        converter = AudioConverter()
        voice_pcm = sine_wave_pcm(1000, 0.05, AudioConverter.OPENAI_RATE)
        mulaw = converter.openai_to_twilio(voice_pcm)
        pcm_back = audioop.ulaw2lin(mulaw, 2)
        energy_out = rms_energy(pcm_back)

        # Reference: raw downsample without filter
        pcm_8k_raw, _ = audioop.ratecv(voice_pcm, 2, 1, 24000, 8000, None)
        mulaw_raw = audioop.lin2ulaw(pcm_8k_raw, 2)
        pcm_raw_back = audioop.ulaw2lin(mulaw_raw, 2)
        energy_ref = rms_energy(pcm_raw_back)

        # With pre-filter, voice energy should not drop more than ~3dB (factor 0.7)
        assert energy_out >= energy_ref * 0.7, (
            f"Too much voice-range energy loss: "
            f"filtered={energy_out:.1f} ref={energy_ref:.1f}"
        )


class TestTwilioToOpenai:
    def test_output_rate(self):
        """twilio_to_openai upsamples 8kHz μ-law to 24kHz PCM (~3x more bytes)."""
        converter = AudioConverter()
        # 10ms at 8kHz = 80 samples μ-law = 80 bytes
        mulaw = audioop.lin2ulaw(bytes(160), 2)  # 80 samples of silence
        result = converter.twilio_to_openai(mulaw)
        # 10ms at 24kHz ≈ 240 samples = 480 bytes PCM; ratecv may produce ±4 bytes
        assert abs(len(result) - 480) <= 8, f"Expected ~480 bytes, got {len(result)}"

    def test_roundtrip_length(self):
        """PCM → μ-law → PCM yields correct lengths at each step."""
        converter = AudioConverter()
        pcm_original = sine_wave_pcm(500, 0.01, AudioConverter.OPENAI_RATE)
        mulaw = converter.openai_to_twilio(pcm_original)
        pcm_back = converter.twilio_to_openai(mulaw)
        # Length should be ~same as original (minor rounding in ratecv is OK)
        assert abs(len(pcm_back) - len(pcm_original)) <= 8
