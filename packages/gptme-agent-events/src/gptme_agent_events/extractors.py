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
from dataclasses import dataclass, field
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
_CODEX_WORKDIR_RE = re.compile(r"""\bworkdir["']?\s*:\s*(["'])((?:\\.|(?!\1).)*)\1""", re.DOTALL)
_SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash"}


def _codex_output(out: Any) -> tuple[int | None, bool, str]:
    """(exit code, still running, text) of a Codex tool result.

    The ``exec`` tool returns a list of text blocks, one holding
    ``JSON.stringify`` of the exec_command result; read its ``exit_code`` and
    ``output`` directly instead of regex-matching an escaped dump of it.
    """
    if isinstance(out, list):
        blocks = [
            b["text"] if isinstance(b, dict) else b
            for b in out
            if isinstance(b, str) or (isinstance(b, dict) and isinstance(b.get("text"), str))
        ]
    elif isinstance(out, str):
        blocks = [out]
    else:
        blocks = [json.dumps(out)]
    code: int | None = None
    inner_running = False
    texts = []
    for block in blocks:
        block_code: int | None = None
        block_text = block
        try:
            data = json.loads(block)
        except ValueError:
            data = None
        if isinstance(data, dict) and ("exit_code" in data or "output" in data):
            raw_code = data.get("exit_code")
            # The legacy ``shell`` tool nests the status under ``metadata``:
            # {"output": ..., "metadata": {"exit_code": 1}}. Reading only the
            # top level made a failed push look landed.
            if raw_code is None and isinstance(data.get("metadata"), dict):
                raw_code = data["metadata"].get("exit_code")
            if isinstance(raw_code, bool):
                raw_code = None
            if raw_code is None and data.get("session_id") is not None:
                # exec_command hit its yield_time_ms with the process still
                # going (slow hooks) even though the outer cell completed.
                inner_running = True
            if isinstance(raw_code, int):
                block_code = raw_code
            elif isinstance(raw_code, str) and re.fullmatch(r"-?\d+", raw_code.strip()):
                # Codex serialises the code as a string in some rollouts. Not
                # reading it made a failed push look like a landed one, because
                # the exit regex only scanned the output payload.
                block_code = int(raw_code.strip())
            if isinstance(data.get("output"), str):
                block_text = data["output"]
        if block_code is None:
            # Legacy text/metadata formats still carry their status in the raw
            # block. Read each result separately so a sibling cannot mask it.
            m = _CODEX_EXIT_RE.search(block_text) or _CODEX_EXIT_RE.search(block)
            block_code = int(m.group(1)) if m else None
        if block_code is not None and code in (None, 0):
            code = block_code
        texts.append(block_text)
    text = "\n".join(texts)
    # A completed sibling says nothing about a command that is still running.
    # The first block is the harness's own header: a yielded cell is running even
    # when results it already printed carry an exit code.
    cell_yielded = bool(blocks) and _CODEX_RUNNING_RE.match(blocks[0].lstrip()) is not None
    running = (
        inner_running
        or cell_yielded
        or (code is None and text.lstrip().startswith(("Script running", "Process running")))
    )
    return code, running, text


_CODEX_RUNNING_RE = re.compile(r"Script running with cell ID (\w+)")
_CODEX_PROCESS_RE = re.compile(r"Process running with session ID (\w+)")
_CODEX_CONTINUE_RE = re.compile(r"\b(write_stdin|wait)\s*\(\s*(\{[^{}]*\})", re.DOTALL)


def _codex_running_keys(out: Any) -> set[tuple[str, str]]:
    """Continuation handles, distinct for outer JS cells and inner processes."""
    text = result_text(out) if isinstance(out, (str, list)) else json.dumps(out)
    keys = {("cell", m.group(1)) for m in _CODEX_RUNNING_RE.finditer(text)}
    keys.update(("process", m.group(1)) for m in _CODEX_PROCESS_RE.finditer(text))
    blocks = out if isinstance(out, list) else [out]
    for block in blocks:
        if isinstance(block, dict) and "text" in block:
            block = block["text"]
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except ValueError:
                continue
        if isinstance(block, dict) and block.get("exit_code") is None:
            sid = block.get("session_id")
            if isinstance(sid, (int, str)) and not isinstance(sid, bool):
                keys.add(("process", str(sid)))
    return keys


def _codex_continuation(name: str, text: str) -> tuple[str, str] | None:
    """Read a direct poll, or one literal poll inside a JS exec cell.

    Mixed cells cannot safely inherit a command's landing kinds: the result
    blocks have no call ids connecting them to the individual JS statements.
    """
    try:
        args = json.loads(text)
    except ValueError:
        args = None
    if not isinstance(args, dict):
        matches = list(_CODEX_CONTINUE_RE.finditer(text)) if name == "exec" else []
        if len(matches) != 1 or _CODEX_CMD_RE.search(text):
            return None
        name = matches[0].group(1)
        try:
            args = json.loads(matches[0].group(2))
        except ValueError:
            # JS object literals commonly leave keys unquoted. Read only a
            # literal handle, never evaluate the cell's source.
            field = "session_id" if name == "write_stdin" else "cell_id"
            handle = re.search(
                rf"(?:\{{|,)\s*{field}\s*:\s*(\d+|\"[^\"\\]*\"|'[^'\\]*')\s*(?=,|\}})",
                matches[0].group(2),
            )
            if not handle:
                return None
            args = {field: handle.group(1).strip("\"'")}
    field, kind = ("session_id", "process") if name == "write_stdin" else ("cell_id", "cell")
    sid = args.get(field) if name in ("write_stdin", "wait") else None
    return (kind, str(sid)) if isinstance(sid, (int, str)) and not isinstance(sid, bool) else None


@dataclass
class _Suspended:
    """A call whose commands outlived it, waiting on every continuation handle."""

    pending: tuple[str, list[str], str | None, str | None]
    open: set[tuple[str, str]] = field(default_factory=set)
    failed: bool = False
    output: str = ""
    reported: bool = False


class CodexExtractor(Extractor):
    """Codex rollouts: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``."""

    harness = "codex"

    def __init__(self, session: str, agent: str = "", subagent: bool = False) -> None:
        super().__init__(session, agent, subagent)
        # handle -> the command waiting on it; poll call id -> (command, handle)
        self._running: dict[tuple[str, str], _Suspended] = {}
        self._continued: dict[str, tuple[_Suspended, tuple[str, str]]] = {}

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
            if call_id in self._pending and workdir:
                # Relative workdirs belong to the turn cwd, never the follower's
                # process cwd. Without a turn cwd we cannot resolve them safely.
                if not os.path.isabs(workdir):
                    workdir = (
                        os.path.normpath(os.path.join(self.cwd, workdir)) if self.cwd else None
                    )
                if workdir:
                    self._pending[call_id] = (*self._pending[call_id][:3], workdir)
            continuation = _codex_continuation(name, text)
            if call_id and continuation is not None and continuation in self._running:
                suspended = self._running.pop(continuation)
                self._pending[call_id] = suspended.pending
                self._continued[call_id] = (suspended, continuation)
            if name == "apply_patch":
                m = _CODEX_PATCH_PATH_RE.search(text)
                if m:
                    ev.detail["path"] = m.group(1).strip()
            return [ev]
        if ptype in ("function_call_output", "custom_tool_call_output") and call_id:
            return self._codex_result(ts, call_id, payload.get("output"))
        return []

    def _codex_result(self, ts: Any, call_id: str, output: Any) -> list[AgentEvent]:
        """Report a call's outcome once every command it started has finished."""
        code, running, text = _codex_output(output)
        failed = code is not None and code != 0
        polled: tuple[str, str] | None = None
        if call_id in self._continued:
            suspended, polled = self._continued.pop(call_id)
            if suspended.reported:
                self._pending.pop(call_id, None)
                return []
            failed = failed or suspended.failed
            text = suspended.output + text
        elif running and call_id in self._pending:
            suspended = _Suspended(self._pending[call_id])
        else:
            return [] if running else self._result(ts, call_id, failed, text)

        if failed and not running:
            # No sibling can turn a failure into a landing, and it may never be
            # polled on its own: report the error now, once.
            suspended.reported = True
            self._running = {k: v for k, v in self._running.items() if v is not suspended}
            return self._result(ts, call_id, True, text)
        suspended.failed = failed
        # Retain only public-linkable identity lines across polls, not an
        # unbounded buffer of command stdout.
        identity = [m.group(0) for m in _COMMIT_LINE_RE.finditer(text)]
        identity += [m.group(0) for m in _PR_URL_RE.finditer(text)]
        suspended.output = "\n".join(identity) + "\n"
        # Transfer the original command/workdir to every continuation handle. A
        # poll gets a new call id, not a new command. A running result that lists
        # no handle leaves the polled one open, so the call is never reported.
        keys = _codex_running_keys(output) if running else set()
        if polled and (keys or not running):
            suspended.open.discard(polled)
        suspended.open |= keys
        self._running.update(dict.fromkeys(keys, suspended))
        if running or suspended.open:
            self._pending.pop(call_id, None)
            return []
        return self._result(ts, call_id, failed, text)

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
        """The call's own ``workdir``, when every command in it names the same one."""
        try:
            args = json.loads(text)
        except ValueError:
            args = None
        if isinstance(args, dict):
            wd = args.get("workdir")
            return wd if isinstance(wd, str) and wd else None
        found = set()
        named = 0
        for m in _CODEX_WORKDIR_RE.finditer(text):
            try:
                if m.group(1) == '"':
                    found.add(json.loads('"' + m.group(2) + '"'))
                else:
                    # Decode the escapes JS single-quoted paths commonly use,
                    # without evaluating source or corrupting Unicode paths.
                    found.add(re.sub(r"\\(['\\])", r"\1", m.group(2)))
            except ValueError:
                return None
            named += 1
        # A command without its own workdir runs in the session cwd, so one
        # workdir only applies when every command in the cell names it.
        if named < len(_CODEX_CMD_RE.findall(text)):
            return None
        return found.pop() if len(found) == 1 else None


EXTRACTORS: dict[str, type[Extractor]] = {
    e.harness: e for e in (ClaudeCodeExtractor, GptmeExtractor, CodexExtractor)
}
