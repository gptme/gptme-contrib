"""Sound sink: hear an agent work, using gptme's sound effects.

Each event maps to one of gptme's bundled ``.wav`` files (the same ones gptme
plays for its own tool calls and turn-complete ding). Files are played with
the system player (``afplay``, ``paplay`` or ``ffplay``) in the background, so
a burst of tool calls never blocks the stream.

Tool sounds are throttled: during a burst only one plays per ``min_gap``
seconds, and at most ``max_concurrent`` sounds overlap. Milestones (turn end,
commit, push, PR, error, session start) are never dropped for spacing.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .event import AgentEvent

DEFAULT_SOUNDS: dict[str, str] = {
    "session_start": "chime",
    "turn_end": "bell",
    "commit": "chime_clean_ring",
    "push": "chime_complex_ring",
    "pr_open": "chime_improved",
    "error": "chime_beating",
    "tool:read": "page_turn",
    "tool:search": "page_turn",
    "tool:write": "file_write",
    "tool:shell": "seashell_click",
    "tool:test": "drilling",
    "tool:web": "page_turn",
    "tool:agent": "sawing",
    "tool:other": "sawing",
}


def sound_key(event: AgentEvent) -> str:
    return f"tool:{event.tool_class or 'other'}" if event.kind == "tool" else event.kind


def find_sounds_dir() -> Path | None:
    """Locate gptme's bundled sounds: importable gptme, else the gptme-util install."""
    try:
        media_path = Path(importlib.import_module("gptme.util.sound").media_path)
        if media_path.is_dir():
            return media_path
    except (ImportError, AttributeError):
        pass
    exe = shutil.which("gptme-util")
    if not exe:
        return None
    python = Path(os.path.realpath(exe)).parent / "python"
    if not python.exists():
        return None
    try:
        out = subprocess.run(
            [str(python), "-c", "from gptme.util.sound import media_path; print(media_path)"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out) if out and Path(out).is_dir() else None


def player_command(path: Path, volume: float) -> list[str] | None:
    """Command to play ``path`` at ``volume`` (0-1) with an available system player."""
    if shutil.which("afplay"):
        return ["afplay", "-v", f"{volume:.2f}", str(path)]
    if shutil.which("paplay"):
        return ["paplay", f"--volume={int(volume * 65536)}", str(path)]
    if shutil.which("ffplay"):
        vol = str(int(volume * 100))
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", vol, str(path)]
    return None


class SoundPlayer:
    def __init__(
        self,
        sounds_dir: Path,
        *,
        mapping: dict[str, str] | None = None,
        volume: float = 0.5,
        agent_volume: dict[str, float] | None = None,
        min_gap: float = 0.15,
        max_concurrent: int = 3,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[list[str]], subprocess.Popen | None] | None = None,
    ) -> None:
        self.sounds_dir = sounds_dir
        self.mapping = {**DEFAULT_SOUNDS, **(mapping or {})}
        self.volume = volume
        self.agent_volume = agent_volume or {}
        self.min_gap = min_gap
        self.max_concurrent = max_concurrent
        self.dry_run = dry_run
        self.clock = clock
        self._spawn = spawn or self._popen
        self._inflight: list[subprocess.Popen] = []
        self._last_tool = float("-inf")

    @staticmethod
    def _popen(cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def handle(self, event: AgentEvent) -> str | None:
        """Play the sound for ``event``; returns its name, or None if skipped."""
        name = self.mapping.get(sound_key(event))
        if not name:
            return None
        self._inflight = [p for p in self._inflight if p.poll() is None]
        now = self.clock()
        if event.kind == "tool":
            if now - self._last_tool < self.min_gap or len(self._inflight) >= self.max_concurrent:
                return None
            self._last_tool = now
        elif len(self._inflight) >= self.max_concurrent * 2:
            return None
        path = self.sounds_dir / f"{name}.wav"
        volume = self.volume * self.agent_volume.get(event.agent, 1.0)
        if self.dry_run:
            print(f"{event.agent or '-'} {sound_key(event)} -> {name}", file=sys.stderr)
            return name
        cmd = player_command(path, volume)
        if cmd is None or not path.exists():
            return None
        proc = self._spawn(cmd)
        if proc is not None:
            self._inflight.append(proc)
        return name
