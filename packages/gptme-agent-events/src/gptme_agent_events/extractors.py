"""Per-harness extractors: one trajectory record in, zero or more events out.

Each extractor is stateful per trajectory file. It remembers tool calls that
are still waiting for a result, so a commit, push or PR is only reported once
its tool result shows it succeeded, and a failed call becomes an ``error``.

Only metadata is read: tool names, call ids, stop reasons, exit status. Shell
commands are inspected in memory to classify them (test run, commit, push) but
are never copied into an event.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from typing import Any

from gptme_sessions.shell_parse import command_repo_dir

from .classify import classify_tool, landing_kinds
from .event import AgentEvent, normalize_ts
from .remote import github_remote_slug

_COMMIT_LINE_RE = re.compile(r"^\[[^\]\n]*?\b([0-9a-f]{7,40})\]", re.MULTILINE)
_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/\d+")


def github_slug(directory: str) -> str | None:
    """``owner/name`` of a local checkout's origin; cache successes only."""
    try:
        return _github_slug(directory)
    except LookupError:
        return None


@lru_cache(maxsize=2048)
def _github_slug(directory: str) -> str:
    # lru_cache does not cache exceptions, allowing failed lookups to recover.
    if not os.path.isabs(directory) or not os.path.isdir(directory):
        raise LookupError(directory)
    try:
        url = subprocess.run(
            ["git", "-C", directory, "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise LookupError(directory) from exc
    slug = github_remote_slug(url)
    if not slug:
        raise LookupError(directory)
    return slug


# Preserve the cache invalidation hook used by callers and tests.
github_slug.cache_clear = _github_slug.cache_clear  # type: ignore[attr-defined]


def landing_detail(kind: str, command: str | None, cwd: str | None, output: str) -> dict:
    """Public-linkable facts about a landed commit/push/PR: repo slug, sha, url."""
    detail: dict[str, str] = {}
    if kind == "pr_open":
        m = _PR_URL_RE.search(output)
        if m:
            detail = {"repo": m.group(1), "url": m.group(0)}
        return detail
    directory = command_repo_dir(command or "", cwd)
    if directory:
        detail["dir"] = directory  # private: where the commit/push ran
    # Only git's remote-tracking reflog identifies a push destination reliably.
    # Shell-only pushes remain unlinked rather than assuming origin.
    slug = github_slug(directory) if directory and kind == "commit" else None
    if slug:
        detail["repo"] = slug
    if kind == "commit":
        m = _COMMIT_LINE_RE.search(output)
        if m:
            detail["sha"] = m.group(1)
    return detail


def result_text(content: Any) -> str:
    """Flatten a tool result (string, or list of text blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


class Extractor:
    harness = ""

    def __init__(self, session: str, agent: str = "", subagent: bool = False) -> None:
        self.session = session
        self.agent = agent
        self.subagent = subagent
        self.cwd: str | None = None
        # call id -> (tool name, kinds to emit on success, command, cwd)
        self._pending: dict[str, tuple[str, list[str], str | None, str | None]] = {}

    def feed(self, rec: dict[str, Any]) -> list[AgentEvent]:
        raise NotImplementedError

    def _ev(self, kind: str, ts: Any, **kw: Any) -> AgentEvent:
        kw.setdefault("subagent", self.subagent)
        if self.cwd:
            # Where the agent is working: private cwd, plus the repo it belongs to.
            context: dict[str, str] = {"cwd": self.cwd}
            slug = github_slug(self.cwd)
            if slug and kind not in ("commit", "push", "pr_open"):
                context["repo"] = slug
            kw["detail"] = {**context, **(kw.get("detail") or {})}
        return AgentEvent(
            kind=kind,
            ts=normalize_ts(ts),
            harness=self.harness,
            session=self.session,
            agent=self.agent,
            **kw,
        )

    def _tool(self, ts: Any, name: str, command: str | None, call_id: str | None, **kw: Any):
        """Emit a tool event and remember what its success would mean."""
        cls = classify_tool(self.harness, name, command)
        kinds = landing_kinds(command) if cls in ("shell", "test") else []
        if call_id:
            self._pending[call_id] = (name, kinds, command if kinds else None, self.cwd)
        return self._ev("tool", ts, tool=name, tool_class=cls, **kw), kinds

    def _landed(
        self,
        ts: Any,
        name: str,
        kinds: list[str],
        command: str | None,
        cwd: str | None,
        output: str,
        **kw: Any,
    ) -> list[AgentEvent]:
        return [
            self._ev(k, ts, tool=name, detail=landing_detail(k, command, cwd, output), **kw)
            for k in kinds
        ]

    def _result(
        self, ts: Any, call_id: str, failed: bool, output: str = "", **kw: Any
    ) -> list[AgentEvent]:
        if call_id not in self._pending:
            return []
        name, kinds, command, cwd = self._pending.pop(call_id)
        if failed:
            return [self._ev("error", ts, tool=name, **kw)]
        return self._landed(ts, name, kinds, command, cwd, output, **kw)


class ClaudeCodeExtractor(Extractor):
    """Claude Code transcripts: ``~/.claude/projects/<slug>/<session>.jsonl``."""

    harness = "claude-code"

    def __init__(self, session: str, agent: str = "", subagent: bool = False) -> None:
        super().__init__(session, agent, subagent)
        # One assistant message is written as several rows (one per content
        # block) that all carry its stop_reason; report the turn end once.
        self._last_ended: str | None = None

    def feed(self, rec: dict[str, Any]) -> list[AgentEvent]:
        rtype = rec.get("type")
        if rtype not in ("assistant", "user"):
            return []
        sid = rec.get("sessionId")
        if isinstance(sid, str) and sid:
            self.session = sid
        ts = rec.get("timestamp")
        if isinstance(rec.get("cwd"), str):
            self.cwd = rec["cwd"]
        sub = {"subagent": True} if rec.get("isSidechain") else {}
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return []
        content = msg.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        out: list[AgentEvent] = []

        if rtype == "assistant":
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                inp = block.get("input")
                inp = inp if isinstance(inp, dict) else {}
                command = inp.get("command") if name == "Bash" else None
                path = inp.get("file_path") or inp.get("notebook_path")
                detail = {"path": path} if isinstance(path, str) else {}
                ev, _ = self._tool(
                    ts,
                    name,
                    command if isinstance(command, str) else None,
                    block.get("id") if isinstance(block.get("id"), str) else None,
                    detail=detail,
                    **sub,
                )
                if ev.tool_class != "write":
                    ev.detail.pop("path", None)  # paths only for writes
                out.append(ev)
            if msg.get("stop_reason") == "end_turn":
                mid = msg.get("id")
                if not (isinstance(mid, str) and mid == self._last_ended):
                    self._last_ended = mid if isinstance(mid, str) else None
                    out.append(self._ev("turn_end", ts, **sub))
        else:
            for block in blocks:
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    out += self._result(
                        ts,
                        block["tool_use_id"],
                        bool(block.get("is_error")),
                        result_text(block.get("content")),
                        **sub,
                    )
        return out


# gptme tool-format calls: "@shell(call_abc): {json args}"
_GPTME_CALL_RE = re.compile(r"\n@(\w+)\(([^)\s]+)\):\s*(.*)", re.DOTALL)
# gptme markdown-format calls: ```shell / ```save path.py
_GPTME_FENCE_RE = re.compile(
    r"^```(\w+)(?:[ \t]+(\S+))?[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL
)
# gptme's own result framing, outside the fenced stdout/stderr blocks: a tool
# exception ("Error executing tool ...") or a shell status line. Matching only
# there keeps a line like "error: ..." printed by a hook during a successful
# commit from turning the commit into an error.
_GPTME_FENCED_RE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_GPTME_STATUS_RE = re.compile(r"^(?:Return code: -?[1-9]\d*|Process interrupted\b)", re.MULTILINE)


def gptme_result_failed(content: str) -> bool:
    if content.lstrip().startswith("Error"):
        return True
    return bool(_GPTME_STATUS_RE.search(_GPTME_FENCED_RE.sub("", content)))


class GptmeExtractor(Extractor):
    """gptme conversation logs: ``<logs>/<conversation>/conversation.jsonl``."""

    harness = "gptme"

    def feed(self, rec: dict[str, Any]) -> list[AgentEvent]:
        role = rec.get("role")
        ts = rec.get("timestamp")
        content = rec.get("content")
        content = content if isinstance(content, str) else ""
        if role == "system":
            call_id = rec.get("call_id")
            if isinstance(call_id, str):
                failed = gptme_result_failed(content)
                return self._result(ts, call_id, failed, content)
            return []
        if role != "assistant":
            return []

        out: list[AgentEvent] = []
        calls = self._tool_calls(content)
        for name, call_id, command, path in calls:
            ev, kinds = self._tool(ts, name, command, call_id)
            if ev.tool_class == "write" and path:
                ev.detail["path"] = path
            out.append(ev)
            if call_id is None:
                # Markdown-format results carry no call id to pair with, so
                # report landings when the call is made.
                out += self._landed(ts, name, kinds, command, self.cwd, "")
        if not calls:
            out.append(self._ev("turn_end", ts))
        return out

    @staticmethod
    def _tool_calls(content: str) -> list[tuple[str, str | None, str | None, str | None]]:
        calls: list[tuple[str, str | None, str | None, str | None]] = []
        for block in re.split(r"(?=\n@\w+\()", "\n" + content):
            m = _GPTME_CALL_RE.match(block)
            if not m:
                continue
            name, call_id, raw = m.group(1), m.group(2), m.group(3)
            try:
                args = json.loads(raw)
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            command = args.get("command") if isinstance(args.get("command"), str) else None
            if name == "gh" and command:
                command = "gh " + command
            path = args.get("path") if isinstance(args.get("path"), str) else None
            calls.append((name, call_id, command, path))
        if calls:
            return calls
        for m in _GPTME_FENCE_RE.finditer(content):
            lang, arg, body = m.group(1), m.group(2), m.group(3)
            if lang not in ("shell", "gh", "save", "append", "patch", "morph", "ipython", "tmux"):
                continue
            command = (
                body if lang in ("shell", "tmux") else ("gh " + body if lang == "gh" else None)
            )
            calls.append((lang, None, command, arg))
        return calls


_CODEX_CMD_RE = re.compile(r"""\bcmd["']?\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.DOTALL)
_CODEX_EXIT_RE = re.compile(
    r"(?:exited with code|exit_code[\"']?\s*:|Exit code:)\s*(-?\d+)", re.IGNORECASE
)
_CODEX_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$", re.MULTILINE)
_CODEX_WORKDIR_KEY_RE = re.compile(r"""\bworkdir["']?\s*:""")
_CODEX_WORKDIR_RE = re.compile(
    r"""\bworkdir["']?\s*:\s*(["'])((?:\\.|(?!\1).)*)\1\s*(?=[,}])""", re.DOTALL
)
_CODEX_RUNNING_RE = re.compile(
    r"^(?:Script running with cell ID|Process running with session ID)\s+\w+\b",
    re.MULTILINE,
)
_SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash"}


def _codex_output(out: Any) -> tuple[int | None, bool, str]:
    """Decode status and stdout without hiding them inside an escaped JSON dump.

    An exec cell may return several text blocks containing JSON.stringify'd
    exec_command results. Any failure wins over sibling successes; any running
    command makes the cell's landing unknown, even if a sibling has finished.
    """
    blocks = out if isinstance(out, list) else [out]
    code: int | None = None
    running = False
    texts: list[str] = []
    for index, block in enumerate(blocks):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block = block["text"]
        text = block if isinstance(block, str) else json.dumps(block)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        raw_code = None
        status_text = text
        if isinstance(data, dict) and any(
            k in data for k in ("exit_code", "output", "metadata", "session_id")
        ):
            raw_code = data.get("exit_code")
            if raw_code is None and isinstance(data.get("metadata"), dict):
                raw_code = data["metadata"].get("exit_code")
            if isinstance(data.get("output"), str):
                text = data["output"]
            block_running = raw_code is None and data.get("session_id") is not None
            running |= block_running
            # A yielded structured process has authoritative running status;
            # status-like stdout must not turn it into a failure.
            status_text = "" if block_running else text
        else:
            # Only the first raw block is tool framing. Later raw blocks are
            # script stdout; structured sibling results above still carry status.
            header = (
                re.split(r"^Output:\s*$", text, maxsplit=1, flags=re.MULTILINE)[0]
                if index == 0
                else ""
            )
            running |= _CODEX_RUNNING_RE.search(header.lstrip()) is not None
            status_text = header
        block_code = None
        if not isinstance(raw_code, bool) and isinstance(raw_code, (int, str)):
            if re.fullmatch(r"-?\d+", str(raw_code).strip()):
                block_code = int(raw_code)
        if block_code is None:
            m = _CODEX_EXIT_RE.search(status_text)
            block_code = int(m.group(1)) if m else None
        if block_code is not None and code in (None, 0):
            code = block_code
        texts.append(text)
    return code, running, "\n".join(texts)


class CodexExtractor(Extractor):
    """Codex rollouts: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``."""

    harness = "codex"

    def feed(self, rec: dict[str, Any]) -> list[AgentEvent]:
        rtype = rec.get("type")
        payload = rec.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        ts = rec.get("timestamp")
        if rtype == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str) and sid:
                self.session = sid
            return []
        if rtype == "turn_context" and isinstance(payload.get("cwd"), str):
            self.cwd = payload["cwd"]
            return []
        if rtype == "event_msg":
            if payload.get("type") == "task_complete":
                return [self._ev("turn_end", ts)]
            return []
        if rtype != "response_item":
            return []

        ptype = payload.get("type")
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None
        if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = str(
                payload.get("name") or ("local_shell" if ptype == "local_shell_call" else "")
            )
            raw = payload.get("arguments", payload.get("input", payload.get("action")))
            text = raw if isinstance(raw, str) else json.dumps(raw)
            ev, _ = self._tool(ts, name, self._command(text), call_id)
            workdir = self._workdir(text)
            if call_id in self._pending and workdir == "":
                # Mixed/partial workdirs cannot safely borrow the session repo.
                # Keep the call for error reporting, but suppress its landings.
                self._pending[call_id] = (name, [], None, None)
            elif call_id in self._pending and workdir:
                # Resolve relative tool workdirs against the turn, never this
                # follower process. Preserve that directory for this call only.
                if not os.path.isabs(workdir):
                    workdir = (
                        os.path.normpath(os.path.join(self.cwd, workdir)) if self.cwd else None
                    )
                if workdir:
                    self._pending[call_id] = (*self._pending[call_id][:3], workdir)
                else:
                    # A relative tool directory without a turn cwd is unknown.
                    self._pending[call_id] = (name, [], None, None)
            if name == "apply_patch":
                m = _CODEX_PATCH_PATH_RE.search(text)
                if m:
                    ev.detail["path"] = m.group(1).strip()
            return [ev]
        if ptype in ("function_call_output", "custom_tool_call_output") and call_id:
            code, running, text = _codex_output(payload.get("output"))
            if code is not None and code != 0:
                return self._result(ts, call_id, True, text)
            if running:
                # No success evidence yet. Continuations have new call ids;
                # leave their eventual landings to reflogs, not guessed status.
                self._pending.pop(call_id, None)
                return []
            return self._result(ts, call_id, code is not None and code != 0, text)
        return []

    @staticmethod
    def _command(text: str) -> str | None:
        """Shell command(s) inside a call: JSON args, or the ``exec`` tool's JS source."""
        try:
            args = json.loads(text)
        except ValueError:
            args = None
        if isinstance(args, dict):
            cmd = args.get("cmd", args.get("command"))
            if isinstance(cmd, list):
                argv = [str(c) for c in cmd]
                # Legacy shell / local_shell_call: ["bash", "-lc", script].
                if (
                    len(argv) == 3
                    and os.path.basename(argv[0]) in _SHELL_WRAPPERS
                    and argv[1].startswith("-")
                    and argv[1].endswith("c")
                ):
                    return argv[2]
                return " ".join(argv)
            if isinstance(cmd, str):
                return cmd
        found = [m.group(2) for m in _CODEX_CMD_RE.finditer(text)]
        return "\n".join(found) if found else None

    @staticmethod
    def _workdir(text: str) -> str | None:
        """Return a shared workdir, None for default, or "" for ambiguous attribution."""
        try:
            args = json.loads(text)
        except ValueError:
            args = None
        if isinstance(args, dict):
            wd = args.get("workdir", args.get("working_directory"))
            return wd if isinstance(wd, str) and wd else None
        found = set()
        commands = list(_CODEX_CMD_RE.finditer(text))
        # Workdir-looking shell text is not a JS argument. Remove command
        # spans before scanning, but count the original commands for safety.
        workdir_text = _CODEX_CMD_RE.sub("", text)
        keys = list(_CODEX_WORKDIR_KEY_RE.finditer(workdir_text))
        if not keys:
            return None
        matches = list(_CODEX_WORKDIR_RE.finditer(workdir_text))
        # Expressions/templates are not statically known directories. Never
        # treat an explicit but undecodable workdir as the session default.
        if len(matches) != len(keys) or len(matches) != len(commands):
            return ""
        for m in matches:
            try:
                wd = (
                    json.loads('"' + m.group(2) + '"')
                    if m.group(1) == '"'
                    else re.sub(r"\\(['\\])", r"\1", m.group(2))
                )
            except ValueError:
                return ""
            found.add(wd)
        if found == {""}:
            return None  # An empty workdir uses the default, as in JSON args.
        return found.pop() if len(found) == 1 else ""


EXTRACTORS: dict[str, type[Extractor]] = {
    e.harness: e for e in (ClaudeCodeExtractor, GptmeExtractor, CodexExtractor)
}
