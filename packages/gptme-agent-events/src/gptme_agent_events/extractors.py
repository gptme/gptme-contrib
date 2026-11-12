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
import re
from typing import Any

from .classify import classify_tool, landing_kinds
from .event import AgentEvent, utc_now


class Extractor:
    harness = ""

    def __init__(self, session: str, agent: str = "", subagent: bool = False) -> None:
        self.session = session
        self.agent = agent
        self.subagent = subagent
        # call id -> (tool name, kinds to emit on success)
        self._pending: dict[str, tuple[str, list[str]]] = {}

    def feed(self, rec: dict[str, Any]) -> list[AgentEvent]:
        raise NotImplementedError

    def _ev(self, kind: str, ts: Any, **kw: Any) -> AgentEvent:
        kw.setdefault("subagent", self.subagent)
        return AgentEvent(
            kind=kind,
            ts=ts if isinstance(ts, str) and ts else utc_now(),
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
            self._pending[call_id] = (name, kinds)
        return self._ev("tool", ts, tool=name, tool_class=cls, **kw), kinds

    def _result(self, ts: Any, call_id: str, failed: bool, **kw: Any) -> list[AgentEvent]:
        if call_id not in self._pending:
            return []
        name, kinds = self._pending.pop(call_id)
        if failed:
            return [self._ev("error", ts, tool=name, **kw)]
        return [self._ev(k, ts, tool=name, **kw) for k in kinds]


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
                    ev.detail = {}
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
                        ts, block["tool_use_id"], bool(block.get("is_error")), **sub
                    )
        return out


# gptme tool-format calls: "@shell(call_abc): {json args}"
_GPTME_CALL_RE = re.compile(r"\n@(\w+)\(([^)\s]+)\):\s*(.*)", re.DOTALL)
# gptme markdown-format calls: ```shell / ```save path.py
_GPTME_FENCE_RE = re.compile(
    r"^```(\w+)(?:[ \t]+(\S+))?[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL
)
_GPTME_ERROR_RE = re.compile(
    r"^(?:Error\b|Return code: -?[1-9]|Process interrupted)", re.MULTILINE | re.IGNORECASE
)


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
                return self._result(ts, call_id, bool(_GPTME_ERROR_RE.search(content[:2000])))
            return []
        if role != "assistant":
            return []

        out: list[AgentEvent] = []
        calls = self._tool_calls(content)
        for name, call_id, command, path in calls:
            ev, kinds = self._tool(ts, name, command, call_id)
            if ev.tool_class == "write" and path:
                ev.detail = {"path": path}
            out.append(ev)
            if call_id is None:
                # Markdown-format results carry no call id to pair with, so
                # report landings when the call is made.
                out += [self._ev(k, ts, tool=name) for k in kinds]
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
            if name == "apply_patch":
                m = _CODEX_PATCH_PATH_RE.search(text)
                if m:
                    ev.detail = {"path": m.group(1).strip()}
            return [ev]
        if ptype in ("function_call_output", "custom_tool_call_output") and call_id:
            out = payload.get("output")
            text = out if isinstance(out, str) else json.dumps(out)
            m = _CODEX_EXIT_RE.search(text)
            return self._result(ts, call_id, bool(m and int(m.group(1)) != 0))
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
                return " ".join(str(c) for c in cmd)
            if isinstance(cmd, str):
                return cmd
        found = [m.group(2) for m in _CODEX_CMD_RE.finditer(text)]
        return "\n".join(found) if found else None


EXTRACTORS: dict[str, type[Extractor]] = {
    e.harness: e for e in (ClaudeCodeExtractor, GptmeExtractor, CodexExtractor)
}
