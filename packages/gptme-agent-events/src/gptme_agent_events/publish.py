"""Publisher sink: a public, privacy-filtered status row per agent.

Consumes the event stream (possibly several agents merged, e.g. a host that
pulls its peers' streams over ssh) and keeps one row per agent up to date in
the gptme.ai ``agent_status`` table (Supabase REST). The row is what the
public "what are the agents doing" feed shows, so only public-safe facts leave
the host:

- ``state`` (working/idle) and the latest ``activity`` class: never task text,
  commands, paths or tool output;
- the latest milestone (commit, push, PR) **only if its repository is public**
  on GitHub (checked via the GitHub API, cached);
- commits to other repositories only as a count over the last hour.

Writes are debounced: milestones go out at once, other changes at most every
``min_interval`` seconds per agent, plus a heartbeat while working so readers
can tell a live agent from a stopped publisher. An agent with no events for
``idle_after`` seconds is published as idle.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .event import AgentEvent

LANDINGS = ("commit", "push", "pr_open")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class RepoVisibility:
    """Is ``owner/name`` a public GitHub repository? Cached; unknown counts as private."""

    def __init__(self, ttl: float = 24 * 3600, token: str | None = None, clock=time.time):
        self.ttl = ttl
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.clock = clock
        self._cache: dict[str, tuple[bool, float]] = {}

    def lookup(self, slug: str) -> bool:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "gptme-agent-events"},
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            return data.get("private") is False and data.get("visibility", "public") == "public"
        except (urllib.error.URLError, OSError, ValueError):
            return False  # 404 (private or missing), rate limit, network: fail closed

    def is_public(self, slug: str | None, *, recheck: bool = False) -> bool:
        if not slug or "/" not in slug:
            return False
        hit = self._cache.get(slug)
        now = self.clock()
        if hit and now - hit[1] < self.ttl and not (recheck and hit[0]):
            return hit[0]
        public = self.lookup(slug)
        self._cache[slug] = (public, now)
        return public


@dataclass
class AgentState:
    last_event: float = 0.0
    activity: str | None = None
    sessions: dict[str, float] = field(default_factory=dict)  # session -> last seen
    milestone: dict | None = None
    private_commits: deque = field(default_factory=deque)  # timestamps
    published: dict | None = None  # last row sent
    last_write: float = 0.0
    urgent: bool = False


class Publisher:
    def __init__(
        self,
        agents: dict[str, str],
        write: Callable[[dict], bool],
        *,
        visibility: RepoVisibility | None = None,
        min_interval: float = 20.0,
        heartbeat: float = 240.0,
        idle_after: float = 300.0,
        session_window: float = 600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``agents`` maps stream agent names to feed agent ids."""
        self.agents = agents
        self.write = write
        self.visibility = visibility or RepoVisibility()
        self.min_interval = min_interval
        self.heartbeat = heartbeat
        self.idle_after = idle_after
        self.session_window = session_window
        self.clock = clock
        self.state: dict[str, AgentState] = {}

    # -- input ----------------------------------------------------------------

    def handle(self, ev: AgentEvent) -> None:
        feed_id = self.agents.get(ev.agent)
        if feed_id is None:
            return
        now = self.clock()
        st = self.state.setdefault(feed_id, AgentState())
        st.last_event = now
        if ev.session and not ev.subagent:
            st.sessions[ev.session] = now
        if ev.kind == "tool" and ev.tool_class:
            st.activity = ev.tool_class
        elif ev.kind in LANDINGS:
            self._landing(st, ev, now)

    def _landing(self, st: AgentState, ev: AgentEvent, now: float) -> None:
        repo = ev.detail.get("repo")
        if not self.visibility.is_public(repo if isinstance(repo, str) else None, recheck=True):
            if st.milestone and st.milestone["repo"] == repo:
                st.milestone = None
                st.urgent = True  # revoke already-published details without debounce
            if ev.kind == "commit":
                st.private_commits.append(now)
            return
        url = None
        if ev.kind == "pr_open" and isinstance(ev.detail.get("url"), str):
            url = ev.detail["url"]
        elif ev.kind == "commit" and isinstance(ev.detail.get("sha"), str):
            url = f"https://github.com/{repo}/commit/{ev.detail['sha']}"
        elif ev.kind == "push":
            ref = str(ev.detail.get("ref") or "")
            branch = ref.split("/", 1)[1] if "/" in ref else ""
            url = (
                f"https://github.com/{repo}/tree/{branch}"
                if branch
                else f"https://github.com/{repo}/"
            )
        st.milestone = {"kind": ev.kind, "repo": repo, "url": url, "at": now}
        st.urgent = True

    # -- output ---------------------------------------------------------------

    def row(self, feed_id: str, st: AgentState, now: float) -> dict:
        while st.private_commits and now - st.private_commits[0] > 3600:
            st.private_commits.popleft()
        for session, seen in list(st.sessions.items()):
            if now - seen > self.session_window:
                del st.sessions[session]
        working = now - st.last_event < self.idle_after
        if st.milestone and not self.visibility.is_public(st.milestone["repo"]):
            st.milestone = None
            st.urgent = True
        m = st.milestone
        return {
            "agent_id": feed_id,
            "state": "working" if working else "idle",
            "activity": st.activity if working else None,
            "milestone_kind": m["kind"] if m else None,
            "milestone_repo": m["repo"] if m else None,
            "milestone_url": m["url"] if m else None,
            "milestone_at": _iso(m["at"]) if m else None,
            "private_commits_1h": len(st.private_commits),
            "active_sessions": len(st.sessions) if working else 0,
        }

    def tick(self) -> int:
        """Write rows that are due; returns how many were written."""
        now = self.clock()
        written = 0
        for feed_id, st in self.state.items():
            row = self.row(feed_id, st, now)
            changed = row != st.published
            if not changed and not (
                row["state"] == "working" and now - st.last_write >= self.heartbeat
            ):
                continue
            idle_flip = st.published is not None and st.published["state"] != row["state"]
            if changed and not (st.urgent or idle_flip) and now - st.last_write < self.min_interval:
                continue
            # Reauthorize retained details before each outgoing row, including
            # heartbeat/idle writes. Cached positives never authorize disclosure.
            if st.milestone and not self.visibility.is_public(st.milestone["repo"], recheck=True):
                st.milestone = None
                row = self.row(feed_id, st, now)
            if self.write(row):
                st.published, st.last_write, st.urgent = row, now, False
                written += 1
        return written


class SupabaseWriter:
    """Upsert rows into ``agent_status`` with an access token from a command."""

    def __init__(self, url: str, anon_key: str, token_cmd: str, token_ttl: float = 45 * 60):
        self.endpoint = url.rstrip("/") + "/rest/v1/agent_status?on_conflict=agent_id"
        self.anon_key = anon_key
        self.token_cmd = token_cmd
        self.token_ttl = token_ttl
        self._token: str | None = None
        self._token_at = 0.0

    def _get_token(self, refresh: bool = False) -> str:
        if refresh or self._token is None or time.time() - self._token_at > self.token_ttl:
            out = subprocess.run(
                shlex.split(self.token_cmd), capture_output=True, text=True, timeout=60, check=True
            ).stdout.strip()
            self._token, self._token_at = out.splitlines()[-1] if out else "", time.time()
        return self._token or ""

    def __call__(self, row: dict) -> bool:
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=json.dumps(row).encode(),
                    method="POST",
                    headers={
                        "apikey": self.anon_key,
                        "Authorization": f"Bearer {self._get_token(refresh=attempt == 1)}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates,return=minimal",
                    },
                )
                with urllib.request.urlopen(req, timeout=15):
                    return True
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._token = None
                    continue  # expired token: refresh once
                print(f"agent-status write failed: HTTP {e.code}", file=sys.stderr)
                return False
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                # Exception messages can include credentials, commands or response bodies.
                print(f"agent-status write failed: {type(e).__name__}", file=sys.stderr)
                return False
        return False
