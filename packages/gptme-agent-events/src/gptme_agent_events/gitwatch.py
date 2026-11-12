"""Commits and pushes from git itself, attributed to sessions by trailer.

Shell output is a weak signal for "did a commit land": wrapper scripts, hooks
and ``| tail`` hide the ``[branch sha]`` line, unattended scripts commit without
any agent tool call, and a commit can be undone a second later. Git's reflogs
record every HEAD movement and every push, so this module tails them:

- ``<common>/logs/HEAD`` and ``<common>/worktrees/*/logs/HEAD``: entries whose
  message starts with ``commit`` become ``commit`` events;
- ``<common>/logs/refs/remotes/**``: ``update by push`` entries become ``push``
  events.

Each commit is attributed to a session through its ``Git-Session-Id`` trailer
(the fleet's session-lineage convention). A push is attributed through the
trailer of the commit it pushed. Events carry ``harness="git"`` and
``detail.source="git"``; the follower relabels the harness when it knows the
session.

Repositories are watched once something points at them: :meth:`GitWatcher.watch`
takes any directory inside a work tree (the follower passes every event's cwd
and every commit's directory). The first read of a reflog keeps only entries
from shortly before the repository was registered, so watching a repository
never replays its history.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .event import AgentEvent, normalize_ts

_REFLOG_RE = re.compile(r"^([0-9a-f]{40}) ([0-9a-f]{40}) .*? (\d+) ([+-]\d{2})(\d{2})\t(.*)$")
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _git(args: list[str], timeout: float = 5) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], check=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _reflog_ts(epoch: str, tz_h: str, tz_m: str) -> str:
    sign = -1 if tz_h.startswith("-") else 1
    offset = timedelta(hours=abs(int(tz_h)), minutes=int(tz_m)) * sign
    dt = datetime.fromtimestamp(int(epoch), timezone(offset))
    return normalize_ts(dt.isoformat())


class GitWatcher:
    def __init__(
        self,
        agent: str = "",
        *,
        slack: float = 120.0,
        tail_bytes: int = 256 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.agent = agent
        self.slack = slack
        self.tail_bytes = tail_bytes
        self.clock = clock
        self._common: dict[str, Path | None] = {}  # directory -> common git dir
        self._repos: dict[Path, tuple[str | None, float]] = {}  # common -> (slug, since)
        self._logs: dict[Path, tuple[int, int]] = {}  # reflog -> (offset, inode)
        self._log_lists: dict[Path, list[Path]] = {}  # common -> its reflog files

    # -- registration --------------------------------------------------------

    def common_dir(self, directory: str) -> Path | None:
        """The shared ``.git`` dir of the repo containing ``directory`` (cached)."""
        if directory in self._common:
            return self._common[directory]
        common: Path | None = None
        if os.path.isabs(directory) and os.path.isdir(directory):
            out = _git(["-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir"])
            if out and os.path.isdir(out):
                common = Path(out)
        self._common[directory] = common
        return common

    def watch(self, directory: str) -> Path | None:
        """Start watching the repo containing ``directory``; returns its common dir."""
        common = self.common_dir(directory)
        if common is not None and common not in self._repos:
            url = _git(["--git-dir", str(common), "config", "--get", "remote.origin.url"])
            m = _GITHUB_REMOTE_RE.search(url or "")
            self._repos[common] = (m.group(1) if m else None, self.clock())
        return common

    def watches(self, directory: str) -> bool:
        common = self.common_dir(directory)
        return common is not None and common in self._repos

    # -- reading -------------------------------------------------------------

    def _reflogs(self, common: Path) -> list[Path]:
        logs = [common / "logs" / "HEAD"]
        logs += sorted(common.glob("worktrees/*/logs/HEAD"))
        remotes = common / "logs" / "refs" / "remotes"
        if remotes.is_dir():
            logs += sorted(p for p in remotes.rglob("*") if p.is_file())
        return [p for p in logs if p.is_file()]

    def _new_lines(self, log: Path, since: float) -> list[str]:
        try:
            st = log.stat()
        except OSError:
            return []
        known = self._logs.get(log)
        if known is None or known[1] != st.st_ino or st.st_size < known[0]:
            # First sight or rewritten (reflog expire): read the tail, keep only
            # entries from around registration onward.
            start = max(0, st.st_size - self.tail_bytes)
            first, floor = True, since - self.slack
        else:
            start, first, floor = known[0], False, 0.0
        if st.st_size == start and not first:
            return []
        try:
            with open(log, "rb") as fh:
                fh.seek(start)
                data = fh.read(st.st_size - start)
        except OSError:
            return []
        complete = data.rfind(b"\n") + 1
        self._logs[log] = (start + complete, st.st_ino)
        lines = data[:complete].decode("utf-8", "replace").splitlines()
        if first and start > 0 and lines:
            lines = lines[1:]  # partial line at the cut
        if not floor:
            return lines
        kept = []
        for line in lines:
            m = _REFLOG_RE.match(line)
            if m and int(m.group(3)) >= floor:
                kept.append(line)
        return kept

    def _session_of(self, common: Path, sha: str) -> str:
        out = _git(
            [
                "--git-dir",
                str(common),
                "log",
                "-1",
                "--format=%(trailers:key=Git-Session-Id,valueonly,separator=%x2C)",
                sha,
            ]
        )
        return (out or "").split(",")[0].strip()

    def poll(self, rediscover: bool = False) -> list[AgentEvent]:
        """New commits/pushes. ``rediscover`` re-lists worktree and remote reflogs."""
        events: list[AgentEvent] = []
        for common, (slug, since) in list(self._repos.items()):
            if rediscover or common not in self._log_lists:
                self._log_lists[common] = self._reflogs(common)
            for log in self._log_lists[common]:
                is_remote = "remotes" in log.relative_to(common).parts
                for line in self._new_lines(log, since):
                    m = _REFLOG_RE.match(line)
                    if not m:
                        continue
                    new, epoch, tz_h, tz_m, message = (m.group(i) for i in (2, 3, 4, 5, 6))
                    if is_remote:
                        if message != "update by push":
                            continue
                        kind = "push"
                    elif message.startswith("commit"):
                        kind = "commit"
                    else:
                        continue
                    detail: dict[str, str] = {"sha": new[:12], "source": "git"}
                    if slug:
                        detail["repo"] = slug
                    if is_remote:
                        ref = log.relative_to(common / "logs" / "refs" / "remotes")
                        detail["ref"] = str(ref)
                    events.append(
                        AgentEvent(
                            kind=kind,
                            ts=_reflog_ts(epoch, tz_h, tz_m),
                            harness="git",
                            session=self._session_of(common, new),
                            agent=self.agent,
                            detail=detail,
                        )
                    )
        return events
