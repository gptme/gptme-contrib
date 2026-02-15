"""
Audio format conversion for voice interface.

Handles conversion between:
- Twilio Media Streams: μ-law 8kHz
- OpenAI Realtime API: PCM 24kHz
"""

import audioop
from typing import Optional


class AudioConverter:
    """Convert between Twilio μ-law 8kHz and OpenAI PCM 24kHz formats."""

    # Twilio uses 8kHz μ-law
    TWILIO_RATE = 8000
    # OpenAI Realtime uses 24kHz PCM
    OPENAI_RATE = 24000

    def __init__(self):
        self._resample_state: Optional[tuple] = None

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

        # Resample from 8kHz to 24kHz (3x upsample)
        pcm_24k, self._resample_state = audioop.ratecv(
            pcm_data,
            2,  # 2 bytes per sample (16-bit)
            1,  # mono
            self.TWILIO_RATE,
            self.OPENAI_RATE,
            self._resample_state,
        )

        return pcm_24k

    def openai_to_twilio(self, pcm_data: bytes) -> bytes:
        """
        Convert OpenAI PCM 24kHz audio to Twilio μ-law 8kHz.

        Args:
            pcm_data: PCM audio at 24kHz (16-bit signed little-endian)

        Returns:
            μ-law encoded audio at 8kHz
        """
        # Resample from 24kHz to 8kHz (3x downsample)
        pcm_8k, _ = audioop.ratecv(
            pcm_data,
            2,  # 2 bytes per sample (16-bit)
            1,  # mono
            self.OPENAI_RATE,
            self.TWILIO_RATE,
            None,
        )

        # Convert linear PCM to μ-law
        mulaw_data = audioop.lin2ulaw(pcm_8k, 2)

        return mulaw_data

    @staticmethod
    def pcm_to_base64(pcm_data: bytes) -> str:
        """Convert PCM bytes to base64 string for JSON transport."""
        import base64

        return base64.b64encode(pcm_data).decode("utf-8")

    @staticmethod
    def base64_to_pcm(base64_str: str) -> bytes:
        """Convert base64 string to PCM bytes."""
        import base64

        return base64.b64decode(base64_str.encode("utf-8"))
