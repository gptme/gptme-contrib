#!/usr/bin/env python3
"""
Voice Interface MVP for gptme.

Provides a minimal voice interface that can:
1. Listen via microphone (local testing)
2. Process through gptme
3. Respond via speaker (local testing)

Future: Twilio integration for phone calls
"""

import argparse
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

# Try to import optional dependencies
try:
    import pyaudio  # type: ignore

    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False

try:
    from openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


def record_audio(duration: int = 5, sample_rate: int = 16000) -> bytes:
    """Record audio from microphone."""
    if not PYAUDIO_AVAILABLE:
        raise RuntimeError("pyaudio not installed. Install with: pip install pyaudio")

    p = pyaudio.PyAudio()

    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        input=True,
        frames_per_buffer=1024,
    )

    print(f"Recording for {duration} seconds...")
    frames = []

    for _ in range(0, int(sample_rate / 1024 * duration)):
        data = stream.read(1024)
        frames.append(data)

    print("Recording complete.")

    stream.stop_stream()
    stream.close()
    p.terminate()

    # Convert to WAV bytes
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wf = wave.open(f.name, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))
        wf.close()

        with open(f.name, "rb") as rf:
            audio_bytes = rf.read()

        Path(f.name).unlink()

    return audio_bytes


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio using OpenAI Whisper API."""
    if not OPENAI_AVAILABLE:
        raise RuntimeError("openai not installed. Install with: pip install openai")

    client = OpenAI()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        temp_path = f.name

    try:
        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return transcript.text
    finally:
        Path(temp_path).unlink()


def speak_text(text: str) -> None:
    """Speak text using system TTS (espeak on Linux, say on macOS)."""
    if sys.platform == "darwin":
        subprocess.run(["say", text], check=True)
    else:
        # Try espeak on Linux
        try:
            subprocess.run(["espeak", text], check=True)
        except FileNotFoundError:
            # Fallback to printing
            print(f"[TTS not available] Would say: {text}")


def process_with_gptme(prompt: str) -> str:
    """Process prompt through gptme CLI."""
    result = subprocess.run(
        ["gptme", "--non-interactive", prompt],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return f"Error: {result.stderr}"

    return result.stdout


def voice_loop(duration: int = 5) -> None:
    """Main voice interaction loop."""
    print("Voice Interface MVP - gptme")
    print("=" * 40)
    print(f"Listening for {duration} seconds after each prompt.")
    print("Press Ctrl+C to exit.\n")

    while True:
        try:
            # Record
            audio = record_audio(duration=duration)

            # Transcribe
            print("Transcribing...")
            text = transcribe_audio(audio)
            print(f"You said: {text}")

            if not text.strip():
                print("No speech detected, listening again...")
                continue

            # Process
            print("Processing with gptme...")
            response = process_with_gptme(text)
            print(f"gptme: {response}")

            # Speak
            speak_text(response)
            print()

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")
            continue


def main():
    parser = argparse.ArgumentParser(description="Voice Interface MVP for gptme")
    parser.add_argument(
        "--duration",
        type=int,
        default=5,
        help="Recording duration in seconds (default: 5)",
    )
    parser.add_argument(
        "--test-tts",
        action="store_true",
        help="Test TTS by speaking a sample phrase",
    )
    parser.add_argument(
        "--test-stt",
        action="store_true",
        help="Test STT by recording and transcribing",
    )

    args = parser.parse_args()

    if args.test_tts:
        speak_text("Hello, this is a test of the gptme voice interface.")
        return

    if args.test_stt:
        audio = record_audio(duration=args.duration)
        text = transcribe_audio(audio)
        print(f"Transcribed: {text}")
        return

    voice_loop(duration=args.duration)


if __name__ == "__main__":
    main()
