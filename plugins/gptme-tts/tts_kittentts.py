#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "kittentts>=0.8.0",
#   "scipy>=1.11.0",
#   "soundfile>=0.12.1",
#   "numpy",
# ]
# ///
"""
KittenTTS backend implementation.

Kitten TTS is an ultra-lightweight, CPU-friendly text-to-speech model
with models ranging from 15M to 80M parameters (25-80 MB on disk).

Model sizes:
  - kitten-tts-mini  (80M,  80 MB) — best quality
  - kitten-tts-micro (40M,  41 MB) — balanced
  - kitten-tts-nano  (15M,  56 MB) — lightweight
  - kitten-tts-nano-int8 (15M, 25 MB) — smallest

Usage:
    from tts_kittentts import KittenTTSBackend
    backend = KittenTTSBackend(model="KittenML/kitten-tts-mini-0.8", voice="Jasper")
    backend.initialize()
    audio = backend.synthesize("Hello world", voice="Jasper")
"""

import io
import logging

import numpy as np
import scipy.io.wavfile as wavfile

log = logging.getLogger(__name__)


# Cache mapping from model name to (model, voice_list) to avoid
# re-downloading on every initialize() call.
_model_cache: dict[str, tuple] = {}


class KittenTTSBackend:
    """KittenTTS backend implementation."""

    def __init__(
        self, model: str = "KittenML/kitten-tts-micro-0.8", voice: str = "Jasper"
    ):
        self.model_name = model
        self.default_voice = voice
        self._model = None
        self._voices: list[str] = []

    def list_voices(self, _lang_code: str | None = None) -> list[str]:
        """List all available voices."""
        return self._voices or [
            "Bella",
            "Jasper",
            "Luna",
            "Bruno",
            "Rosie",
            "Hugo",
            "Kiki",
            "Leo",
        ]

    def close(self) -> None:
        """Close the model and release resources."""
        self._model = None
        log.info("KittenTTS model closed")

    def initialize(self, voice: str | None = None) -> None:
        """Initialize the KittenTTS model."""
        try:
            from kittentts import KittenTTS

            voice_name = voice or self.default_voice

            # Check cache first
            if self.model_name in _model_cache:
                self._model, _ = _model_cache[self.model_name]
                log.info(f"Using cached KittenTTS model: {self.model_name}")
            else:
                log.info(f"Loading KittenTTS model: {self.model_name}...")
                self._model = KittenTTS(self.model_name)
                _model_cache[self.model_name] = (self._model, None)
                log.info(f"KittenTTS model loaded: {self.model_name}")

            # Cache available voices
            self._voices = (
                list(self._model.available_voices)
                if hasattr(self._model, "available_voices")
                else []
            )
            if not self._voices:
                self._voices = [
                    "Bella",
                    "Jasper",
                    "Luna",
                    "Bruno",
                    "Rosie",
                    "Hugo",
                    "Kiki",
                    "Leo",
                ]

            if voice_name not in self._voices:
                log.warning(
                    f"Voice '{voice_name}' not found, using '{self._voices[0]}'. "
                    f"Available: {self._voices}"
                )

            self.default_voice = (
                voice_name if voice_name in self._voices else self._voices[0]
            )
            log.info(
                f"KittenTTS initialized (model: {self.model_name}, voice: {self.default_voice})"
            )

        except ImportError as e:
            raise ImportError(
                f"Failed to import KittenTTS: {e}. "
                "Install with: pip install https://github.com/KittenML/KittenTTS/releases/download/0.8.1/kittentts-0.8.1-py3-none-any.whl"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to initialize KittenTTS: {e}") from e

    def strip_silence(
        self,
        audio_data: np.ndarray,
        threshold: float = 0.01,
        min_silence_duration: int = 1000,
    ) -> np.ndarray:
        """Strip silence from the beginning and end of audio data."""
        abs_audio = np.abs(audio_data)
        mask = abs_audio > threshold

        non_silent = np.where(mask)[0]
        if len(non_silent) == 0:
            return audio_data

        start = max(0, non_silent[0] - min_silence_duration)
        end = min(len(audio_data), non_silent[-1] + min_silence_duration)

        return audio_data[start:end]

    def synthesize(
        self, text: str, voice: str | None = None, speed: float = 1.0
    ) -> io.BytesIO:
        """Convert text to speech and return audio buffer (WAV, 24kHz, 16-bit)."""
        if self._model is None:
            raise RuntimeError("Model not initialized. Call initialize() first.")

        current_voice = voice or self.default_voice
        if current_voice not in self._voices:
            log.warning(
                f"Voice '{current_voice}' not found, falling back to '{self._voices[0]}'"
            )
            current_voice = self._voices[0]

        try:
            log.info(
                f"Generating audio with KittenTTS (voice: {current_voice}, speed: {speed}x)"
            )

            # KittenTTS generate() returns a numpy array at 24kHz
            audio = self._model.generate(
                text, voice=current_voice, speed=speed, clean_text=True
            )

            if audio is None or len(audio) == 0:
                raise ValueError("No audio generated")

            # Strip silence from audio
            audio = self.strip_silence(audio)

            # Normalize to [-1, 1] if needed
            if np.max(np.abs(audio)) > 1.0:
                audio = audio / np.max(np.abs(audio))

            # Convert to 16-bit integer format (standard for WAV files)
            audio_int16 = (audio * 32767).astype(np.int16)

            # Convert to WAV format
            buffer = io.BytesIO()
            wavfile.write(buffer, 24000, audio_int16)
            buffer.seek(0)

            return buffer

        except Exception as e:
            log.error(f"Failed to generate speech with KittenTTS: {e}")
            raise

    def get_info(self) -> dict:
        """Get backend information."""
        try:
            import kittentts

            version = getattr(kittentts, "__version__", "0.8.1")
        except Exception:
            version = "unknown"

        return {
            "name": "kittentts",
            "version": version,
            "model": self.model_name,
            "voice": {
                "default": self.default_voice,
                "available": self.list_voices(),
            },
        }
