#!/usr/bin/env python3
"""Lightweight push-to-talk voice loop for gptme.

Hold Space (or pass --duration N) → speak → gptme answers via TTS.
No telephony stack required.

Requirements (desktop only):
    pip install click sounddevice faster-whisper pynput
"""

# mypy: disable-error-code="import-untyped,import-not-found"

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = os.environ.get("HTT_MODEL", "tiny.en")
HOLD_KEY = os.environ.get("HTT_HOLD_KEY", "space")
GPTME_ARGS = os.environ.get("HTT_GPTME_ARGS", "")
SAMPLE_RATE = 16000  # Hz — Whisper expects 16kHz mono

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


def _require(pkg: str) -> None:
    try:
        __import__(pkg.replace("-", "_"))
    except ImportError:
        print(f"Missing: pip install {pkg}", file=sys.stderr)
        sys.exit(1)


def _tts_command() -> list[str]:
    """Return platform TTS command prefix."""
    override = os.environ.get("HTT_TTS_CMD")
    if override:
        return shlex.split(override)
    if platform.system() == "Darwin":
        return ["say"]
    if shutil.which("espeak"):
        return ["espeak"]
    if shutil.which("espeak-ng"):
        return ["espeak-ng"]
    return []  # silent fallback


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------


def record_while_held(key_name: str = "space") -> Path:
    """Record audio while a key is held down, return path to WAV file."""
    _require("sounddevice")
    _require("pynput")

    import sounddevice as sd
    from pynput import keyboard

    chunks: list = []
    recording = threading.Event()

    key_obj = getattr(keyboard.Key, key_name, None) or keyboard.KeyCode.from_char(
        key_name
    )

    def on_press(k: object) -> None:
        if k == key_obj and not recording.is_set():
            recording.set()
            print("🎤 Recording… (release key to stop)", flush=True)

    def on_release(k: object) -> None:
        if k == key_obj:
            recording.clear()

    print(f"Hold [{key_name.upper()}] to speak. Ctrl-C to quit.")
    with keyboard.Listener(on_press=on_press, on_release=on_release):
        while True:
            recording.wait()
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16"
            ) as stream:
                while recording.is_set():
                    chunk, _ = stream.read(SAMPLE_RATE // 10)  # 100ms blocks
                    chunks.append(chunk)
            if chunks:
                break

    return _save_wav(chunks)


def record_for_duration(seconds: float) -> Path:
    """Record audio for a fixed duration, return path to WAV file."""
    _require("sounddevice")
    import sounddevice as sd

    print(f"🎤 Recording for {seconds}s…", flush=True)
    audio = sd.rec(
        int(seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    return _save_wav([audio])


def _save_wav(chunks: list) -> Path:
    import wave

    import numpy as np

    audio = np.concatenate(chunks, axis=0)
    tmp = tempfile.NamedTemporaryFile(prefix="htt-", suffix=".wav", delete=False)
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# STT — faster-whisper
# ---------------------------------------------------------------------------


def transcribe(wav_path: Path, model_name: str = MODEL) -> str:
    _require("faster_whisper")
    from faster_whisper import WhisperModel

    print(f"📝 Transcribing ({model_name})…", flush=True)
    t0 = time.monotonic()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav_path), beam_size=1)
    text = " ".join(s.text for s in segments).strip()
    ms = int((time.monotonic() - t0) * 1000)
    print(f"   [{ms}ms] {text!r}", flush=True)
    return text


# ---------------------------------------------------------------------------
# gptme call
# ---------------------------------------------------------------------------


def ask_gptme(prompt: str) -> str:
    """Run one gptme turn and return its final assistant message."""
    cmd = ["gptme", "-n", "--output-format", "json", "--tools", "none"]
    cmd.extend(shlex.split(GPTME_ARGS))
    cmd.extend(["--", prompt])
    print("🤖 Asking gptme…", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"gptme exited {result.returncode}: {result.stderr.strip()}")

    last_content = ""
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "message"
            and event.get("role") == "assistant"
            and isinstance(event.get("content"), str)
        ):
            last_content = event["content"]
    if not last_content.strip():
        raise RuntimeError("gptme returned no assistant message")
    return last_content


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


def speak(text: str) -> None:
    cmd = _tts_command()
    if not cmd:
        print(f"(no TTS) {text}")
        return
    print("🔊 Speaking…", flush=True)
    subprocess.run(cmd + [text], check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--duration",
    type=click.FloatRange(min=0.001),
    help="Record for N seconds instead of holding a key.",
)
@click.option(
    "--audio-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Transcribe an existing audio file; leave it unchanged.",
)
@click.option("--model", default=MODEL, show_default=True, help="Whisper model.")
@click.option("--transcript-only", is_flag=True, help="Skip gptme and TTS.")
@click.option("--no-tts", is_flag=True, help="Print the response without speaking it.")
def main(
    duration: float | None,
    audio_file: str | None,
    model: str,
    transcript_only: bool,
    no_tts: bool,
) -> None:
    """Record or read audio, transcribe it locally, and run one gptme turn."""
    if audio_file is not None and duration is not None:
        raise click.UsageError("Use either --audio-file or --duration.")
    wav = (
        Path(audio_file)
        if audio_file is not None
        else (
            record_for_duration(duration)
            if duration is not None
            else record_while_held(HOLD_KEY)
        )
    )
    try:
        text = transcribe(wav, model)
    finally:
        if audio_file is None:
            wav.unlink(missing_ok=True)

    if not text:
        print("(silence — nothing to send)")
        return
    if transcript_only:
        print(text)
        return
    try:
        response = ask_gptme(text)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error
    print(f"\n{response}\n")
    if not no_tts:
        speak(response)


if __name__ == "__main__":
    main()
