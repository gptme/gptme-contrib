"""Follow every live trajectory on this host and turn new lines into events.

Discovery is by location, not by process: each :class:`Source` names a harness,
a root directory and glob patterns. Files that already exist at startup are
followed from their current end, so only new activity is reported (pass
``from_start`` to replay recent files). Files that appear later are read from
the beginning and announce themselves with a ``session_start`` event.

The follower polls with ``stat`` only, so it needs no inotify and works the same
on Linux and macOS. It copes with the ways harnesses write their logs:

- partial trailing lines wait in a buffer until the newline arrives;
- a file replaced or truncated in place (gptme rewrites ``conversation.jsonl``
  through a temporary file after compaction) is re-read from the start, and
  events not newer than the last one already reported are skipped.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .event import AgentEvent, utc_now
from .extractors import EXTRACTORS, Extractor

HARNESSES = tuple(EXTRACTORS)


@dataclass(frozen=True)
class Source:
    harness: str
    root: Path
    patterns: tuple[str, ...]


def claude_code_source(root: Path | None = None) -> Source:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return Source("claude-code", root or base / "projects", ("*/*.jsonl", "*/*/subagents/*.jsonl"))


def gptme_source(root: Path | None = None) -> Source:
    base = os.environ.get("GPTME_LOGS_HOME") or Path.home() / ".local/share/gptme/logs"
    # Agent workspaces may nest per-session log homes one level deeper.
    return Source("gptme", root or Path(base), ("*/conversation.jsonl", "*/*/conversation.jsonl"))


def codex_source(root: Path | None = None) -> Source:
    base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Source("codex", root or base / "sessions", ("*/*/*/*.jsonl",))


SOURCE_FACTORIES: dict[str, Callable[[Path | None], Source]] = {
    "claude-code": claude_code_source,
    "gptme": gptme_source,
    "codex": codex_source,
}


def default_sources() -> list[Source]:
    return [factory(None) for factory in SOURCE_FACTORIES.values()]


def session_name(harness: str, path: Path) -> str:
    """Fallback session id from the path (extractors refine it from content)."""
    return path.parent.name if harness == "gptme" else path.stem


@dataclass
class _File:
    harness: str
    path: Path
    offset: int
    inode: int
    is_new: bool
    buf: bytes = b""
    last_ts: str = ""
    skip_until: str = ""
    extractor: Extractor | None = None
    mtime: float = 0.0


class Follower:
    def __init__(
        self,
        sources: list[Source],
        agent: str = "",
        *,
        active_window: float = 600.0,
        rescan_interval: float = 5.0,
        from_start: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sources = sources
        self.agent = agent
        self.active_window = active_window
        self.rescan_interval = rescan_interval
        self.from_start = from_start
        self.clock = clock
        self._files: dict[Path, _File] = {}
        self._last_scan: float | None = None

    # -- discovery ---------------------------------------------------------

    def _scan(self) -> None:
        first = self._last_scan is None
        now = self.clock()
        self._last_scan = now
        for src in self.sources:
            if not src.root.is_dir():
                continue
            for pattern in src.patterns:
                for path in src.root.glob(pattern):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if path in self._files:
                        self._files[path].mtime = st.st_mtime
                        continue
                    recent = now - st.st_mtime <= self.active_window
                    replay = first and self.from_start and recent
                    self._files[path] = _File(
                        harness=src.harness,
                        path=path,
                        offset=0 if (replay or not first) else st.st_size,
                        inode=st.st_ino,
                        is_new=not first,
                        mtime=st.st_mtime,
                    )

    # -- reading -----------------------------------------------------------

    def _extractor(self, f: _File) -> Extractor:
        if f.extractor is None:
            f.extractor = EXTRACTORS[f.harness](
                session_name(f.harness, f.path),
                agent=self.agent,
                subagent="subagents" in f.path.parts,
            )
        return f.extractor

    def _read(self, f: _File) -> list[AgentEvent]:
        try:
            st = f.path.stat()
        except OSError:
            del self._files[f.path]
            return []
        f.mtime = st.st_mtime
        if st.st_ino != f.inode or st.st_size < f.offset:
            # Replaced or truncated: re-read, but only report what is newer.
            f.inode, f.offset, f.buf, f.skip_until = st.st_ino, 0, b"", f.last_ts
        if st.st_size == f.offset:
            return []
        try:
            with open(f.path, "rb") as fh:
                fh.seek(f.offset)
                chunk = fh.read(st.st_size - f.offset)
        except OSError:
            return []
        f.offset += len(chunk)
        data = f.buf + chunk
        lines = data.split(b"\n")
        f.buf = lines.pop()

        ex = self._extractor(f)
        events: list[AgentEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                events += ex.feed(rec)

        if f.skip_until:
            events = [e for e in events if e.ts > f.skip_until]
        if events:
            f.skip_until = ""
            f.last_ts = max(f.last_ts, *(e.ts for e in events))
        if f.is_new and lines:
            f.is_new = False
            if not ex.subagent:  # a subagent is part of its parent's session
                start = AgentEvent(
                    kind="session_start",
                    ts=events[0].ts if events else utc_now(),
                    harness=f.harness,
                    session=ex.session,
                    agent=self.agent,
                )
                events.insert(0, start)
        return events

    def poll(self) -> list[AgentEvent]:
        """One pass: rescan if due, then read new lines from active files."""
        now = self.clock()
        if self._last_scan is None or now - self._last_scan >= self.rescan_interval:
            self._scan()
        events: list[AgentEvent] = []
        for f in list(self._files.values()):
            # Idle files are skipped; the rescan refreshes their mtime.
            if f.is_new or now - f.mtime <= self.active_window:
                events += self._read(f)
        return events

    def run(self, interval: float = 0.5) -> Iterator[AgentEvent]:
        while True:
            yield from self.poll()
            time.sleep(interval)
