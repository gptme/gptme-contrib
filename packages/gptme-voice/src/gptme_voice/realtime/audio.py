"""
Audio format conversion for voice interface.

Handles conversion between:
- Twilio Media Streams: μ-law 8kHz
- OpenAI Realtime API: PCM 24kHz
"""

import audioop
import base64


class AudioConverter:
    """Convert between Twilio μ-law 8kHz and OpenAI PCM 24kHz formats."""

    # Twilio uses 8kHz μ-law
    TWILIO_RATE = 8000
    # Browser AudioWorklet path uses PCM 16kHz
    BROWSER_RATE = 16000
    # OpenAI Realtime uses 24kHz PCM
    OPENAI_RATE = 24000

    def __init__(self):
        # Keyed by input_rate so Twilio (8kHz) and Browser (16kHz) paths
        # don't corrupt each other's resampler state when called on the same instance.
        self._resample_states: dict[int, tuple | None] = {}

    def pcm_to_openai(self, pcm_data: bytes, input_rate: int) -> bytes:
        """
        Convert linear PCM audio to OpenAI PCM 24kHz.

        Args:
            pcm_data: PCM audio at ``input_rate`` (16-bit signed little-endian)
            input_rate: Sample rate of ``pcm_data``

        Returns:
            PCM audio at 24kHz (16-bit signed little-endian)
        """
        if input_rate == self.OPENAI_RATE:
            return pcm_data

        pcm_24k, new_state = audioop.ratecv(
            pcm_data,
            2,  # 2 bytes per sample (16-bit)
            1,  # mono
            input_rate,
            self.OPENAI_RATE,
            self._resample_states.get(input_rate),
        )
        self._resample_states[input_rate] = new_state
        return pcm_24k

    def twilio_to_openai(self, mulaw_data: bytes) -> bytes:
        """
        Convert Twilio μ-law 8kHz audio to OpenAI PCM 24kHz.

        Args:
            mulaw_data: μ-law encoded audio at 8kHz

        Returns:
            PCM audio at 24kHz (16-bit signed little-endian)
        """
        # Convert μ-law to linear PCM (16-bit)
        pcm_data = audioop.ulaw2lin(mulaw_data, 2)
        return self.pcm_to_openai(pcm_data, self.TWILIO_RATE)

    def browser_to_openai(self, pcm_data: bytes) -> bytes:
        """
        Convert browser PCM 16kHz audio to OpenAI PCM 24kHz.

        Args:
            pcm_data: PCM audio at 16kHz (16-bit signed little-endian)

        Returns:
            PCM audio at 24kHz (16-bit signed little-endian)
        """
        return self.pcm_to_openai(pcm_data, self.BROWSER_RATE)

    def openai_to_twilio(self, pcm_data: bytes) -> bytes:
        """
        Convert OpenAI PCM 24kHz audio to Twilio μ-law 8kHz.

        Args:
            pcm_data: PCM audio at 24kHz (16-bit signed little-endian)

        Returns:
            μ-law encoded audio at 8kHz
        """
        # Anti-aliasing pre-filter: gentle IIR low-pass at 24kHz before downsampling.
        # weightA=2, weightB=1 gives fc≈4.7kHz (-3dB) at 24kHz sample rate.
        # This attenuates 5–12kHz spectral energy by 3–6dB, reducing fold-back
        # artifacts when downsampling 3x to 8kHz. Voice content (0–3.4kHz) loses
        # at most ~2dB. Uses no extra dependencies (audioop is stdlib).
        pcm_filtered, _ = audioop.ratecv(
            pcm_data,
            2,  # 2 bytes per sample (16-bit)
            1,  # mono
            self.OPENAI_RATE,
            self.OPENAI_RATE,
            None,
            2,  # weightA
            1,  # weightB
        )

        # Resample from 24kHz to 8kHz (3x downsample)
        pcm_8k, _ = audioop.ratecv(
            pcm_filtered,
            2,  # 2 bytes per sample (16-bit)
            1,  # mono
            self.OPENAI_RATE,
            self.TWILIO_RATE,
            None,
        )

        # Convert linear PCM to μ-law
        return audioop.lin2ulaw(pcm_8k, 2)

    @staticmethod
    def pcm_to_base64(pcm_data: bytes) -> str:
        """Convert PCM bytes to base64 string for JSON transport."""
        return base64.b64encode(pcm_data).decode("utf-8")

    @staticmethod
    def base64_to_pcm(base64_str: str) -> bytes:
        """Convert base64 string to PCM bytes."""
        return base64.b64decode(base64_str.encode("utf-8"))
