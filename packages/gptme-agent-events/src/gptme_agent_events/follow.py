"""Follow every live trajectory on this host and turn new lines into events.

Discovery is by location, not by process: each :class:`Source` names a harness,
a root directory and glob patterns. Files that already exist at startup are
followed from their current end, so only new activity is reported (pass
``from_start`` to replay recent files). Files that appear later are read from
the beginning and announce themselves with a ``session_start`` event.

Hosts accumulate tens of thousands of old trajectories, so discovery is
tiered. A quick scan (every ``rescan_interval``) lists only the roots and
"warm" directories: those modified within ``warm_horizon`` or newly appeared.
A quick scan re-lists a directory only if its
mtime changed (new entries). A full sweep (every ``full_scan_interval``) walks everything, re-checks idle
files so a resumed session is picked up, and recomputes which directories are
warm. Only files active within ``active_window`` are kept open for polling;
idle ones are kept as a compact (offset, inode) record.

Files that appear after startup are read from the start, but events older than
their discovery time (minus ``discovery_slack``) are dropped, so a conversation
directory that is copied or moved into place does not replay its history as
live activity.

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
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from .event import AgentEvent
from .extractors import EXTRACTORS, Extractor
from .gitwatch import GitWatcher

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


def gptme_workspace(logdir: Path) -> str | None:
    """A gptme conversation's workspace: the ``workspace`` link, else config.toml."""
    link = logdir / "workspace"
    if link.is_symlink() or link.is_dir():
        return os.path.realpath(link)
    try:
        for line in (logdir / "config.toml").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "workspace":
                return os.path.expanduser(value.strip().strip("\"'"))
    except OSError:
        pass
    return None


@dataclass
class _File:
    harness: str
    path: Path
    offset: int
    inode: int
    mtime: float
    announce: bool = False  # emit session_start before the first kept event
    not_before: str = ""  # drop events older than this (files found after startup)
    buf: bytes = b""
    last_ts: str = ""
    skip_until: str = ""
    extractor: Extractor | None = None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


class Follower:
    def __init__(
        self,
        sources: list[Source],
        agent: str = "",
        *,
        active_window: float = 600.0,
        rescan_interval: float = 5.0,
        full_scan_interval: float = 300.0,
        warm_horizon: float = 86400.0,
        discovery_slack: float = 120.0,
        from_start: bool = False,
        git: GitWatcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sources = sources
        self.git = git
        self._session_harness: dict[str, str] = {}
        # Tool results can arrive a poll after the corresponding reflog entry.
        self._git_shell: list[tuple[float, Path, AgentEvent]] = []
        self._git_pending: list[tuple[float, Path, AgentEvent]] = []
        self.agent = agent
        self.active_window = active_window
        self.rescan_interval = rescan_interval
        self.full_scan_interval = full_scan_interval
        self.warm_horizon = warm_horizon
        self.discovery_slack = discovery_slack
        self.from_start = from_start
        self.clock = clock
        self._files: dict[Path, _File] = {}  # followed (recently active)
        self._cold: dict[Path, tuple[int, int]] = {}  # idle: (offset, inode)
        self._children: dict[Path, set[str]] = {}  # directory listings
        self._warm: set[Path] = set()  # directories quick scans descend into
        self._listed_mtime: dict[Path, float] = {}  # directory mtime at last listing
        self._last_scan: float | None = None
        self._last_full: float | None = None

    # -- discovery ---------------------------------------------------------

    def _walk(self, directory: Path, parts: tuple[str, ...], full: bool, now: float):
        """Yield files under ``directory`` matching ``parts``, pruning cold dirs on quick scans.

        Quick scans don't re-list a directory whose mtime is unchanged since its
        last listing: no entries were added, so there is nothing new to find
        there (appends to known files are seen by polling or the full sweep).
        """
        try:
            mtime = directory.stat().st_mtime  # before listing: a racing add re-lists next time
        except OSError:
            return
        if not full and self.rescan_interval > 0 and self._listed_mtime.get(directory) == mtime:
            if len(parts) > 1:
                # Iterate the (small) warm set, not the (possibly huge) listing.
                for sub in [w for w in self._warm if w.parent == directory]:
                    if fnmatch(sub.name, parts[0]):
                        yield from self._walk(sub, parts[1:], full, now)
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        known = self._children.get(directory)
        self._children[directory] = {e.name for e in entries}
        self._listed_mtime[directory] = mtime
        if len(parts) == 1:
            for e in entries:
                if fnmatch(e.name, parts[0]) and e.is_file():
                    yield Path(e.path)
            return
        for e in entries:
            if not fnmatch(e.name, parts[0]) or not e.is_dir():
                continue
            sub = Path(e.path)
            if full:
                try:
                    recent = now - e.stat().st_mtime <= self.warm_horizon
                except OSError:
                    continue
                if recent:
                    self._warm.add(sub)
                else:
                    self._warm.discard(sub)
            elif known is None or e.name not in known:
                self._warm.add(sub)  # appeared since the last listing
            if full or sub in self._warm:
                yield from self._walk(sub, parts[1:], full, now)

    def _scan(self, full: bool) -> None:
        now = self.clock()
        first = self._last_full is None
        self._last_scan = now
        if full:
            self._last_full = now
        seen: set[Path] = set()
        for src in self.sources:
            if not src.root.is_dir():
                continue
            for pattern in src.patterns:
                for path in self._walk(src.root, Path(pattern).parts, full, now):
                    seen.add(path)
                    self._consider(src.harness, path, full, first, now)
        if full:
            for gone in [p for p in self._cold if p not in seen]:
                del self._cold[gone]

    def _consider(self, harness: str, path: Path, full: bool, first: bool, now: float) -> None:
        if path in self._files or (path in self._cold and not full):
            return  # followed files are polled; idle ones are re-checked on full sweeps
        try:
            st = path.stat()
        except OSError:
            return
        if path in self._cold:
            offset, inode = self._cold[path]
            if st.st_ino == inode and st.st_size == offset:
                return
            del self._cold[path]
            appended = st.st_ino == inode and st.st_size > offset
            self._files[path] = _File(
                harness,
                path,
                offset=offset if appended else 0,
                inode=st.st_ino,
                mtime=st.st_mtime,
                not_before="" if appended else _iso(now - self.discovery_slack),
            )
            return
        if first:
            if now - st.st_mtime > self.active_window:
                self._cold[path] = (st.st_size, st.st_ino)
                return
            offset = 0 if self.from_start else st.st_size
            self._files[path] = _File(harness, path, offset, st.st_ino, st.st_mtime)
            return
        # Appeared after startup: a new session, or history moved into place.
        self._files[path] = _File(
            harness,
            path,
            offset=0,
            inode=st.st_ino,
            mtime=st.st_mtime,
            announce="subagents" not in path.parts,  # subagents belong to their parent
            not_before=_iso(now - self.discovery_slack),
        )

    # -- reading -----------------------------------------------------------

    def _extractor(self, f: _File) -> Extractor:
        if f.extractor is None:
            f.extractor = EXTRACTORS[f.harness](
                session_name(f.harness, f.path),
                agent=self.agent,
                subagent="subagents" in f.path.parts,
            )
            if f.harness == "gptme":
                f.extractor.cwd = gptme_workspace(f.path.parent)
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
        lines = (f.buf + chunk).split(b"\n")
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
        if f.not_before:
            events = [e for e in events if e.ts >= f.not_before]
        if events:
            f.skip_until = ""
            f.last_ts = max(f.last_ts, *(e.ts for e in events))
            if f.announce:
                f.announce = False
                start = AgentEvent(
                    kind="session_start",
                    ts=events[0].ts,
                    harness=f.harness,
                    session=ex.session,
                    agent=self.agent,
                )
                events.insert(0, start)
        return events

    def poll(self) -> list[AgentEvent]:
        """One pass: scan if due, read new lines from followed files, retire idle ones."""
        now = self.clock()
        scanned = True
        if self._last_full is None or now - self._last_full >= self.full_scan_interval:
            self._scan(full=True)
        elif self._last_scan is None or now - self._last_scan >= self.rescan_interval:
            self._scan(full=False)
        else:
            scanned = False
        events: list[AgentEvent] = []
        for f in list(self._files.values()):
            events += self._read(f)
            if f.path in self._files and now - f.mtime > self.active_window:
                del self._files[f.path]
                self._cold[f.path] = (f.offset - len(f.buf), f.inode)
        for e in events:
            self._session_harness[e.session] = e.harness
        if self.git is not None:
            events = self._with_git(events, rediscover=scanned)
        return events

    def _with_git(self, events: list[AgentEvent], rediscover: bool) -> list[AgentEvent]:
        """Use git's reflogs as the source of truth for commits and pushes."""
        assert self.git is not None
        for e in events:
            for key in ("dir", "cwd"):
                if isinstance(e.detail.get(key), str):
                    self.git.watch(e.detail[key])
        now = self.clock()
        self._git_shell = [item for item in self._git_shell if now - item[0] <= 120]
        kept = []
        for e in events:
            directory = str(e.detail.get("dir") or e.detail.get("cwd") or "")
            common = self.git.common_dir(directory) if e.kind in ("commit", "push") else None
            if common is not None and self.git.watches(directory):
                self._git_shell.append((now, common, e))
            else:
                kept.append(e)
        for e in self.git.poll(rediscover=rediscover):
            self._git_pending.append((now, Path(e.detail["dir"]), e))
        pending = []
        # Count from the original batch, not the shrinking list as matches are
        # consumed: proximity alone must be unique in both directions.
        batch = list(self._git_pending)
        for seen, common, e in batch:
            matches = []
            for item in self._git_shell:
                when, repo, shell = item
                if repo != common or shell.kind != e.kind:
                    continue
                sha = str(shell.detail.get("sha") or "")
                # A commit hash is stronger evidence than proximity. Push output
                # has no hash: only use a unique result in a short polling window.
                if e.kind == "commit" and sha:
                    git_sha = str(e.detail["sha"])
                    if not (git_sha.startswith(sha) or sha.startswith(git_sha)):
                        continue
                else:
                    if e.session and shell.session != e.session:
                        continue
                    if abs(when - seen) > 5:
                        continue
                    possible = [
                        other
                        for stamp, repo, other in batch
                        if repo == common
                        and other.kind == shell.kind
                        and abs(when - stamp) <= 5
                        and (not other.session or other.session == shell.session)
                    ]
                    if len(possible) != 1:
                        continue
                matches.append(item)
            if len(matches) == 1:
                item = matches[0]
                shell = item[2]
                if not e.session:
                    e.session, e.harness = shell.session, shell.harness
                    e.tool, e.subagent = shell.tool, shell.subagent
                self._git_shell.remove(item)
            elif now - seen < 5:
                pending.append((seen, common, e))
                continue
            e.harness = self._session_harness.get(e.session, e.harness)
            kept.append(e)
        self._git_pending = pending
        # If no reflog exists (e.g. core.logAllRefUpdates=false), retain the
        # shell evidence rather than silently losing the event altogether.
        waiting = []
        for seen, common, e in self._git_shell:
            if now - seen >= 5:
                kept.append(e)
            else:
                waiting.append((seen, common, e))
        self._git_shell = waiting
        return kept

    def run(self, interval: float = 0.5) -> Iterator[AgentEvent]:
        while True:
            yield from self.poll()
            time.sleep(interval)
