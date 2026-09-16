"""Per-session subagent summary derived from parent + child trajectories.

Phase 1.1 of the session-record subagent fields: compute a ``subagent_summary``
dict from an already-resolved :class:`~gptme_sessions.transcript.SessionTree`
(or equivalent parent/child records). The classifier and wait/concurrency
metrics are the 2026-09-10 analysis port
(``scripts/analysis/subagent_usage.py``), so backfill matches that baseline.

Codex child-rollout resolution (``thread_spawn.parent_thread_id``) is phase 1.3
and is not done here — Codex parents still get ``session_kind`` / ``active_seconds``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# read-only vs acting classifier (ported from the 2026-09-10 analysis)
# ---------------------------------------------------------------------------
# A command segment starts at ^, after ; & | ( or $( — so
# `sed -n '1,9p' bin/git-safe-commit` (reading a script) is not a commit,
# while `cd X && git-safe-commit ...` is.
_SEG = (
    r"(?:^|[;&|(\n]|\$\()\s*(?:sudo\s+|env\s+\S+\s+|uv\s+run\s+|python3?\s+|"
    r"bash\s+|timeout\s+\S+\s+|nice\s+\S*\s*|xargs\s+\S*\s*)*"
)
_STRONG_RE = re.compile(
    "(?x)"
    + _SEG
    + r"""(?:
    git\s+(?:-C\s+\S+\s+)?(commit|push|merge|rebase|cherry-pick|reset|stash|am|apply|
        worktree\s+(add|remove|prune)|checkout\s+-b|switch\s+-c|branch\s+-[dD]|tag\s|rm\s|mv\s)(?![\w-])
  | git\s+(?:-C\s+\S+\s+)?add\s
  | git-safe-commit\b | git-safe-push-master\b
  | gh\s+(pr\s+(create|merge|close|edit|comment|review|ready|checkout)
          | issue\s+(create|close|edit|comment|reopen|transfer)
          | release\s+create
          | api\b(?![^|;\n]*-X\s*GET)(?=[^|;\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--method\s*(?:POST|PUT|PATCH|DELETE)|\s-[fF]\s|--input\b)))
  | gptodo\s+(edit|add|set|new|create|archive|generate-queue)
  | (?:\S*/)?coordination\s+work-(claim|complete) | (?:\S*/)?claim-github-issue\.py | (?:\S*/)?vent\.py
  | (?:\S*/)?schedule-recheck\.sh | (?:\S*/)?[\w-]*remember\.py | gptme-util\s+memory\s+(save|supersede)
  | (?:pip|uv)\s+(install|add|remove|sync|tool\s+install) | npm\s+(install|run|ci)\b | pnpm\s | yarn\s
  | systemctl\s+(--user\s+)?(start|stop|restart|enable|disable|reload|kill|reset-failed)
  | pkill\b | kill\s+-?\d | crontab\s
  | make\s+(format|install|build|deploy)\b | ruff\s+[^|]*--fix | prek\s+run[^|]*(?:--fix|format)
  | curl\s(?=[^|;\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|\s-d\s|--data\b|--upload-file))
  | agent-msg\s+send | gptmail\s+\S*\s*send
)"""
)
_PY_WRITE_RE = re.compile(
    r"""(?x)open\(\s*[^)]*,\s*['"](?:w|a|wb|ab)['"] | \.write_text\( | \.write_bytes\(
      | \bshutil\.(copy|move|rmtree) | \bos\.(remove|rename|unlink|makedirs) | \.unlink\( | \.mkdir\("""
)
_FILEOPS_RE = re.compile(
    _SEG
    + r"(?:rm|mv|cp|mkdir|touch|chmod|chown|ln|rmdir|truncate|tee|install)\s|\bsed\s+-i|\bcat\s*>"
)
_DOWNLOAD_RE = re.compile(r"\bcurl\s[^|;]*\s(?:-o|--output)\s+(\S+)|\bwget\s[^|;]*\s-O\s+(\S+)")
_QUOTED_RE = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
# User-agnostic home dir — contrib must not hardcode an agent username.
_HOME_DIR = r"(?:~|/home/[^/]+)"
_CD_SCRATCH_RE = re.compile(
    r"\bcd\s+(?:/tmp(?:/(?!worktrees/)|\b)|\$SCRATCH|\"?\$D\b|"
    + _HOME_DIR
    + r"/\.cache/|/dev/shm/|"
    + _HOME_DIR
    + r"/\.claude/projects/\S+/tool-results)"
)
_ABS_REPO_PATH_RE = re.compile(
    r"(?<![\w./-])("
    + _HOME_DIR
    + r"/(?!\.cache|\.claude/projects/\S+/tool-results)"
    + r"|\$REPO_ROOT|/tmp/worktrees/)"
)
_PRELUDE_RE = re.compile(
    r"REPO_ROOT=\$\(git rev-parse --show-toplevel\)|cd\s+(?:\$REPO_ROOT|"
    + _HOME_DIR
    + r"/\S+)\s*(?:&&|;)?"
)
_REDIRECT_RE = re.compile(
    r"(?<![0-9&<>=!])>{1,2}(?!=)\s*(?!&|/dev/null|/dev/stderr)"
    r"((?:[~$/.]|\w+[/.])[^\s;|&)>\]'\"]*)"
)
_SCRATCH_PATH_RE = re.compile(
    r"^(/tmp/(?!worktrees/)|/dev/shm/|/run/user/|"
    + _HOME_DIR
    + r"/\.cache/"
    + r"|\$SCRATCH|\$\{?TMP|\$OUT\b|\$T\b|\$D\b)"
)
_NONSCRATCH_PATH_RE = re.compile(
    r"(?<![\w./-])(/tmp/worktrees/|" + _HOME_DIR + r"/(?!\.cache)|\$REPO_ROOT|\$REPO\b|"
    r"\./|journal/|tasks/|knowledge/|lessons/|state/|scripts/"
    r"|packages/|gptme-contrib/)"
)
_QUOTED_TARGET_RE = re.compile(r"(?<![0-9&<>=!])>{1,2}\s*[\"']([^\"'\n]+)[\"']")
_CD_ANY_RE = re.compile(r"(?:^|[;&|]\s*)cd\s+(\S+)", re.M)

_CC_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
_ACTIVE_GAP_CAP_S = 15 * 60
_NOTIF_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_NOTIF_TAGS = {
    k: re.compile(rf"<{k}>(.*?)</{k}>", re.S)
    for k in (
        "task-id",
        "tool-use-id",
        "status",
        "summary",
        "result",
        "subagent_tokens",
        "tool_uses",
        "duration_ms",
    )
}

# Transcript sentinels used when session-records are not available (run.sh
# preambles). Order matters: first match wins.
_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("project monitoring session", "pm"),
    ("pm-react", "pm"),
    ("SESSION_SENTINEL", "autonomous"),
    ("autonomous work session", "autonomous"),
    ("focused task in a worktree", "worker"),
    ("strategic idea generator", "oneshot"),
    ("Reply with exactly", "oneshot"),
)

EMPTY_SUMMARY: dict[str, Any] = {
    "subagents_total": 0,
    "subagents_depth_max": 0,
    "subagents_max_concurrent": 0,
    "subagents_readonly": 0,
    "subagents_scratch": 0,
    "subagents_acting": 0,
    "subagent_tokens_total": 0,
    "subagent_seconds_total": 0,
    "subagent_tool_output_bytes": 0,
    "subagent_report_bytes": 0,
    "subagent_resumes": 0,
    "spawns_parent_kept_working": 0,
    "spawns_total": 0,
    "parent_idle_max_seconds": 0,
    "active_seconds": 0,
    "session_kind": "unknown",
}


def empty_summary() -> dict[str, Any]:
    """Zeroed summary dict (always the same keys)."""
    return dict(EMPTY_SUMMARY)


def cmd_mutation(cmd: str, cwd_scratch: bool = False) -> tuple[str | None, str | None]:
    """Return ``(label, evidence)`` — label in ``{'acting','scratch',None}``.

    ``cwd_scratch`` says the shell already sits in scratch space (Claude Code's
    Bash keeps its cwd between calls; Codex runs each exec in a fresh shell).
    """
    unq = _QUOTED_RE.sub("''", cmd)
    m = _STRONG_RE.search(unq)
    if m:
        return "acting", unq[max(0, m.start() - 20) : m.end() + 40]
    targets = list(_REDIRECT_RE.findall(unq))
    targets += list(_QUOTED_TARGET_RE.findall(cmd))
    for dl in _DOWNLOAD_RE.finditer(cmd):
        t = (dl.group(1) or dl.group(2) or "").strip("'\"")
        if t and not t.startswith("/dev/"):
            targets.append(t)
    sh = _FILEOPS_RE.search(unq)
    py = _PY_WRITE_RE.search(cmd)
    fm = sh or py
    in_scratch = bool(_CD_SCRATCH_RE.search(cmd)) or (cwd_scratch and not _CD_ANY_RE.search(cmd))
    if not targets and not fm:
        return None, None
    body = _PRELUDE_RE.sub("", cmd)
    if sh:
        repo_hit = _ABS_REPO_PATH_RE.search(body) or (
            None if in_scratch else _NONSCRATCH_PATH_RE.search(_PRELUDE_RE.sub("", unq))
        )
    else:
        repo_hit = _ABS_REPO_PATH_RE.search(body)
    if fm and repo_hit:
        return "acting", cmd[max(0, fm.start() - 20) : fm.end() + 60]
    for t in targets:
        if _SCRATCH_PATH_RE.match(t):
            continue
        if t.startswith("/") or t.startswith("~") or _NONSCRATCH_PATH_RE.search(t):
            return "acting", f"redirect > {t}"
        if not t.startswith("$") and not in_scratch:
            return "acting", f"redirect > {t}"
    ev = ("fileop " + cmd[max(0, fm.start()) : fm.end() + 50]) if fm else f"redirect > {targets[0]}"
    return "scratch", ev


def classify_agent(
    tools: dict[str, int],
    bash_cmds: list[str],
    write_paths: list[str],
    sticky_cwd: bool = True,
) -> tuple[str, list[str]]:
    """Classify a subagent as ``readonly`` / ``scratch`` / ``acting``.

    ``tools`` is accepted for analysis-script compatibility; classification is
    driven by write paths and shell commands (Explore agents can mutate via Bash).
    """
    del tools  # signature compatibility with the analysis script
    label = "readonly"
    ev: list[str] = []
    cwd_scratch = False
    for path in write_paths:
        if _SCRATCH_PATH_RE.match(path) or "/scratchpad/" in path:
            if label == "readonly":
                label = "scratch"
            ev.append(f"write(scratch) {path}")
        else:
            label = "acting"
            ev.append(f"write {path}")
    for cmd in bash_cmds:
        lab, evidence = cmd_mutation(cmd, cwd_scratch)
        if sticky_cwd:
            last_cd = None
            for m in _CD_ANY_RE.finditer(cmd):
                last_cd = m.group(1)
            if last_cd is not None:
                cwd_scratch = bool(_CD_SCRATCH_RE.search("cd " + last_cd))
        if lab == "acting":
            label = "acting"
            ev.append(evidence or cmd[:80])
        elif lab == "scratch" and label == "readonly":
            label = "scratch"
            ev.append(evidence or cmd[:80])
    return label, ev[:6]


def max_concurrency(intervals: list[tuple[float, float]]) -> int:
    """Sweep-line maximum overlap of ``(start, end)`` intervals."""
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((max(end, start), -1))
    # Starts before ends at equal timestamps: a zero-width interval (single
    # record) must count as concurrent with itself (max >= 1), not 0.
    events.sort(key=lambda item: (item[0], -item[1]))
    current = best = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


def infer_session_kind(
    *,
    harness: str | None,
    entrypoint: str,
    first_prompt: str,
    originator: str = "",
) -> str:
    """Classify interactive vs autonomous from transcript hints (no records join)."""
    for needle, kind in _KIND_RULES:
        if needle in first_prompt:
            return kind
    origin = originator or entrypoint
    if harness in ("claude-code", "claude_code"):
        return "interactive" if entrypoint == "cli" else "oneshot"
    if harness == "codex":
        return "interactive" if origin in ("codex-tui", "codex_interactive") else "oneshot"
    if harness in ("grok", "grok-build"):
        return "autonomous"
    if harness == "gptme":
        return "autonomous" if first_prompt else "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# record scanning
# ---------------------------------------------------------------------------


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _record_ts(record: dict[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if isinstance(raw, str):
        parsed = _ts(raw)
        if parsed is not None:
            return parsed
    payload = record.get("payload")
    if isinstance(payload, dict):
        raw = payload.get("timestamp")
        if isinstance(raw, str):
            return _ts(raw)
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text") or "")
                elif item.get("type") == "tool_result":
                    parts.append(_text_of(item.get("content")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _notif_tag(body: str, key: str) -> str:
    match = _NOTIF_TAGS[key].search(body)
    return (match.group(1) if match else "").strip()


def _collect_notifications(
    text: str, ts: float | None, seen: set[tuple[str, int, int]], dest: list[dict[str, Any]]
) -> None:
    for match in _NOTIF_RE.finditer(text):
        body = match.group(1)
        if not _notif_tag(body, "summary").startswith("Agent"):
            continue
        key = (
            _notif_tag(body, "task-id"),
            int(_notif_tag(body, "duration_ms") or 0),
            len(_notif_tag(body, "result")),
        )
        if key in seen:
            continue
        seen.add(key)
        dest.append(
            {
                "ts": ts,
                "task_id": _notif_tag(body, "task-id"),
                "tool_use_id": _notif_tag(body, "tool-use-id"),
                "text_len": len(body),
                "result_len": len(_notif_tag(body, "result")),
            }
        )


@dataclass
class TranscriptScan:
    """Lightweight scan of one transcript (parent or child)."""

    first_ts: float | None = None
    last_ts: float | None = None
    tools: dict[str, int] = field(default_factory=dict)
    bash_cmds: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    agent_calls: list[dict[str, Any]] = field(default_factory=list)
    sendmessages: int = 0
    notifs: list[dict[str, Any]] = field(default_factory=list)
    result_bytes: int = 0
    final_text_len: int = 0
    entrypoint: str = ""
    originator: str = ""
    first_prompt: str = ""
    turn_ts: list[float] = field(default_factory=list)
    spawn_turn_ts: set[float] = field(default_factory=set)
    active_s: float = 0.0
    tokens_total: int = 0


def _bump_active(scan: TranscriptScan, ts: float | None, prev: float | None) -> float | None:
    if ts is None:
        return prev
    if scan.first_ts is None:
        scan.first_ts = ts
    scan.last_ts = ts
    if prev is not None:
        scan.active_s += min(ts - prev, _ACTIVE_GAP_CAP_S)
    return ts


def _scan_cc(records: list[dict[str, Any]]) -> TranscriptScan:
    scan = TranscriptScan()
    prev: float | None = None
    seen_msg: set[str] = set()
    seen_notif: set[tuple[str, int, int]] = set()
    for record in records:
        rec_type = record.get("type")
        ts = _record_ts(record)
        if rec_type == "attachment":
            att = record.get("attachment") or {}
            if att.get("type") == "queued_command" and "<task-notification>" in (
                att.get("prompt") or ""
            ):
                _collect_notifications(att.get("prompt") or "", ts, seen_notif, scan.notifs)
            continue
        if rec_type == "session_meta":
            payload = record.get("payload") or {}
            if isinstance(payload, dict) and payload.get("originator"):
                scan.originator = str(payload.get("originator"))
        if rec_type not in ("assistant", "user"):
            continue
        prev = _bump_active(scan, ts, prev)
        if not scan.entrypoint and record.get("entrypoint"):
            scan.entrypoint = str(record.get("entrypoint"))
        msg = record.get("message") or {}
        content = msg.get("content")
        if rec_type == "assistant":
            mid = str(msg.get("id") or record.get("requestId") or record.get("uuid") or "")
            is_new_turn = not mid or mid not in seen_msg
            if mid:
                seen_msg.add(mid)
            if is_new_turn and ts is not None:
                scan.turn_ts.append(ts)
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        scan.final_text_len = len(item.get("text") or "")
                    if item.get("type") != "tool_use":
                        continue
                    name = str(item.get("name") or "?")
                    inp = item.get("input") or {}
                    scan.tools[name] = scan.tools.get(name, 0) + 1
                    if name == "Bash":
                        scan.bash_cmds.append(str(inp.get("command") or ""))
                    elif name in _CC_WRITE_TOOLS:
                        scan.write_paths.append(
                            str(inp.get("file_path") or inp.get("notebook_path") or "?")
                        )
                    elif name == "Agent":
                        spawn_ts = ts
                        scan.agent_calls.append(
                            {
                                "id": item.get("id"),
                                "ts": spawn_ts,
                                "type": inp.get("subagent_type") or "default",
                            }
                        )
                        if spawn_ts is not None:
                            scan.spawn_turn_ts.add(spawn_ts)
                    elif name == "SendMessage":
                        scan.sendmessages += 1
        else:
            # A resumed/queued transcript can open with a tool_result payload;
            # that is tool output, not the user's prompt — skip it.
            first_is_tool_result = isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in content
            )
            if not scan.first_prompt and not first_is_tool_result:
                scan.first_prompt = _text_of(content)[:400]
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        # Count extracted text, not json.dumps of content-block lists.
                        scan.result_bytes += len(_text_of(item.get("content")))
            text = _text_of(content)
            if "<task-notification>" in text:
                _collect_notifications(text, ts, seen_notif, scan.notifs)
    return scan


def _scan_codex(records: list[dict[str, Any]]) -> TranscriptScan:
    """Best-effort Codex scan for kind/active time and (if present) children."""
    scan = TranscriptScan()
    prev: float | None = None
    for record in records:
        rec_type = record.get("type")
        ts = _record_ts(record)
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if rec_type == "session_meta" and isinstance(payload, dict):
            origin = payload.get("originator")
            if origin:
                scan.originator = str(origin)
            source = payload.get("source")
            if isinstance(source, dict):
                spawn = (source.get("subagent") or {}).get("thread_spawn") or {}
                if spawn.get("parent_thread_id"):
                    scan.agent_calls.append({"id": spawn.get("parent_thread_id"), "ts": ts})
        if ts is not None and rec_type in (
            "session_meta",
            "response_item",
            "event_msg",
            "turn_context",
        ):
            prev = _bump_active(scan, ts, prev)
        if rec_type != "response_item" or not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if ptype == "message":
            if payload.get("role") == "assistant" and ts is not None:
                scan.turn_ts.append(ts)
            if payload.get("role") == "user" and not scan.first_prompt:
                scan.first_prompt = _text_of(payload.get("content"))[:400]
        if ptype == "function_call":
            name = str(payload.get("name") or "")
            scan.tools[name] = scan.tools.get(name, 0) + 1
            args = payload.get("arguments")
            parsed: dict[str, Any] = {}
            if isinstance(args, dict):
                parsed = args
            elif isinstance(args, str):
                try:
                    loaded = json.loads(args)
                    if isinstance(loaded, dict):
                        parsed = loaded
                except json.JSONDecodeError:
                    parsed = {}
            if name in ("exec_command", "shell_command", "shell"):
                scan.bash_cmds.append(str(parsed.get("command") or parsed.get("cmd") or ""))
            elif name in ("apply_patch", "write_file"):
                scan.write_paths.append(str(parsed.get("path") or parsed.get("file_path") or "?"))
            elif name == "send_message":
                scan.sendmessages += 1
            elif name == "spawn_agent":
                scan.agent_calls.append({"id": parsed.get("task_name"), "ts": ts})
                if ts is not None:
                    scan.spawn_turn_ts.add(ts)
        if ptype == "agent_message":
            text = _text_of(payload.get("text") or payload.get("content") or "")
            if "FINAL_ANSWER" in text:
                scan.notifs.append(
                    {"ts": ts, "task_id": payload.get("author") or "", "text_len": len(text)}
                )
                scan.final_text_len += len(text)
    return scan


_GPTME_SHELL_LANGS = {"bash", "sh", "shell", "shell-expanded", "ipython"}
_GPTME_WRITE_LANGS = {
    "save",
    "append",
    "patch",
    "insert",
    "replace",
    "touch",
    "mkdir",
    "move",
    "rename",
    "delete",
    "rm",
}


def _scan_gptme(records: list[dict[str, Any]]) -> TranscriptScan:
    """Scan a gptme-format transcript (role + content blocks).

    gptme children write via ``save``/``patch``-style code blocks and mutate via
    shell code blocks, so both feed the classifier — a plain timestamp-only
    scan would classify every writing gptme child as ``readonly``.
    """
    scan = TranscriptScan()
    prev: float | None = None
    for record in records:
        ts = _record_ts(record)
        prev = _bump_active(scan, ts, prev)
        role = record.get("role")
        content = record.get("content")
        if role == "assistant" and ts is not None:
            scan.turn_ts.append(ts)
        if role == "user" and not scan.first_prompt:
            scan.first_prompt = _text_of(content)[:400]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            text = str(block.get("content") or block.get("text") or "")
            if btype == "code":
                lang = str(block.get("lang") or "").lower()
                if lang in _GPTME_WRITE_LANGS:
                    first_line = text.splitlines()[0] if text else "?"
                    scan.write_paths.append(first_line)
                    scan.tools[lang] = scan.tools.get(lang, 0) + 1
                elif lang in _GPTME_SHELL_LANGS:
                    scan.bash_cmds.append(text)
                    scan.tools[lang] = scan.tools.get(lang, 0) + 1
            elif btype == "console":
                scan.result_bytes += len(text)
            elif btype in ("tool_use", "function_call"):
                name = str(block.get("name") or "tool")
                scan.tools[name] = scan.tools.get(name, 0) + 1
                args = block.get("input") or block.get("arguments") or {}
                if isinstance(args, dict):
                    if args.get("command"):
                        scan.bash_cmds.append(str(args["command"]))
                    path = args.get("path") or args.get("file_path")
                    if path:
                        scan.write_paths.append(str(path))
    return scan


def _scan_generic(records: list[dict[str, Any]]) -> TranscriptScan:
    scan = TranscriptScan()
    prev: float | None = None
    for record in records:
        ts = _record_ts(record)
        prev = _bump_active(scan, ts, prev)
        if not scan.first_prompt:
            role = record.get("role")
            if role == "user":
                scan.first_prompt = _text_of(record.get("content"))[:400]
        content = record.get("content")
        if not isinstance(content, list):
            continue
        # Tool-call extraction even for unknown harnesses: without it a child
        # that writes files or runs commands is classified as ``readonly``
        # (labels come from tools/bash_cmds/write_paths), and its tool output
        # bytes are dropped from the summary.
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("tool_use", "function_call"):
                name = str(block.get("name") or "tool")
                scan.tools[name] = scan.tools.get(name, 0) + 1
                args = block.get("input") or block.get("arguments") or {}
                if isinstance(args, dict):
                    if args.get("command"):
                        scan.bash_cmds.append(str(args["command"]))
                    path = args.get("path") or args.get("file_path")
                    if path:
                        scan.write_paths.append(str(path))
            elif btype == "tool_result":
                scan.result_bytes += len(_text_of(block.get("content")))
    return scan


def scan_records(records: list[dict[str, Any]], harness: str | None) -> TranscriptScan:
    """Scan one transcript according to harness."""
    if harness == "codex":
        return _scan_codex(records)
    if harness in ("claude-code", "claude_code"):
        return _scan_cc(records)
    if harness == "gptme":
        return _scan_gptme(records)
    if harness in ("grok", "grok-build", "pi", "copilot"):
        return _scan_generic(records)
    if any(record.get("type") == "session_meta" for record in records):
        return _scan_codex(records)
    if any(record.get("type") == "assistant" for record in records):
        return _scan_cc(records)
    return _scan_generic(records)


def _child_tokens(records: list[dict[str, Any]], harness: str | None) -> int:
    try:
        from .signals import extract_usage_cc, extract_usage_codex, extract_usage_gptme
    except Exception:
        return 0
    usage: dict[str, Any] = {}
    if harness in ("claude-code", "claude_code", None):
        if any(r.get("type") == "assistant" for r in records):
            usage = extract_usage_cc(records)
        elif harness == "codex" or any(r.get("type") == "session_meta" for r in records):
            usage = extract_usage_codex(records)
        else:
            usage = extract_usage_gptme(records)
    elif harness == "codex":
        usage = extract_usage_codex(records)
    elif harness == "gptme":
        usage = extract_usage_gptme(records)
    else:
        usage = extract_usage_cc(records) or extract_usage_codex(records)
    return int(usage.get("total_tokens") or 0)


@dataclass
class ChildSpec:
    """One resolved child transcript plus tree metadata."""

    records: list[dict[str, Any]]
    spawn_depth: int = 1
    session_id: str = ""
    tool_use_id: str | None = None
    agent_type: str | None = None


def _walk_tree_children(tree: Any) -> list[ChildSpec]:
    out: list[ChildSpec] = []

    def walk(nodes: Iterable[Any]) -> None:
        for node in nodes:
            out.append(
                ChildSpec(
                    records=list(getattr(node, "records", []) or []),
                    spawn_depth=int(getattr(node, "spawn_depth", 1) or 1),
                    session_id=str(getattr(node, "session_id", "") or ""),
                    tool_use_id=getattr(node, "tool_use_id", None),
                    agent_type=getattr(node, "agent_type", None),
                )
            )
            walk(getattr(node, "children", []) or [])

    walk(getattr(tree, "subagents", []) or [])
    return out


def summarize_subagents(
    parent_records: list[dict[str, Any]],
    children: list[ChildSpec],
    *,
    harness: str | None = None,
) -> dict[str, Any]:
    """Compute ``subagent_summary`` from parent records and resolved children."""
    parent = scan_records(parent_records, harness)
    summary = empty_summary()
    summary["session_kind"] = infer_session_kind(
        harness=harness,
        entrypoint=parent.entrypoint,
        first_prompt=parent.first_prompt,
        originator=parent.originator,
    )
    summary["active_seconds"] = int(round(parent.active_s))
    summary["subagent_resumes"] = parent.sendmessages
    summary["spawns_total"] = sum(1 for child in children if child.spawn_depth <= 1)
    if not children:
        return summary

    labels: Counter[str] = Counter()
    intervals: list[tuple[float, float]] = []
    depth_max = 0
    tokens_total = 0
    seconds_total = 0
    tool_output_bytes = 0
    child_done_ts: dict[str, float] = {}

    sticky = harness in ("claude-code", "claude_code", None)
    for child in children:
        depth_max = max(depth_max, child.spawn_depth)
        scanned = scan_records(child.records, harness)
        label, _ = classify_agent(
            scanned.tools, scanned.bash_cmds, scanned.write_paths, sticky_cwd=sticky
        )
        labels[label] += 1
        try:
            tokens_total += _child_tokens(child.records, harness)
        except Exception:
            # Best-effort per child: one malformed child's usage extraction
            # must not abort the whole summary (the caller wraps the entire
            # summarize_subagents call, so an unguarded raise would replace
            # every computed field with empty_summary).
            pass
        if scanned.first_ts is not None and scanned.last_ts is not None:
            intervals.append((scanned.first_ts, scanned.last_ts))
            seconds_total += int(round(scanned.last_ts - scanned.first_ts))
            agent_id = child.session_id.removeprefix("agent-")
            child_done_ts[agent_id] = scanned.last_ts
            if child.tool_use_id:
                child_done_ts[str(child.tool_use_id)] = scanned.last_ts
        tool_output_bytes += scanned.result_bytes

    summary["subagents_total"] = len(children)
    summary["subagents_depth_max"] = depth_max
    summary["subagents_max_concurrent"] = max_concurrency(intervals)
    summary["subagents_readonly"] = int(labels.get("readonly", 0))
    summary["subagents_scratch"] = int(labels.get("scratch", 0))
    summary["subagents_acting"] = int(labels.get("acting", 0))
    summary["subagent_tokens_total"] = tokens_total
    summary["subagent_seconds_total"] = seconds_total
    summary["subagent_tool_output_bytes"] = tool_output_bytes
    summary["subagent_report_bytes"] = sum(int(n.get("text_len") or 0) for n in parent.notifs)

    notif_by_id: dict[str, float] = {}
    for notif in parent.notifs:
        for key in (notif.get("task_id"), notif.get("tool_use_id")):
            if key and key not in notif_by_id and notif.get("ts"):
                notif_by_id[str(key)] = float(notif["ts"])

    # Parent-side launch timestamps (from the Agent tool call) are the correct
    # kept-working window start: a non-spawn parent turn between the launch and
    # the child's first emitted record would otherwise be excluded.
    # Keyed match covers metadata toolUseId. Greedy fallback covers Agent
    # calls with no id and children whose metadata is missing — both used
    # to silently start the window at the child's first record.
    launches: list[tuple[float, str | None]] = []
    spawn_ts_by_id: dict[str, float] = {}
    for call in parent.agent_calls:
        call_ts = call.get("ts")
        if call_ts is None:
            continue
        ts = float(call_ts)
        raw_id = call.get("id")
        cid = str(raw_id) if raw_id else None
        launches.append((ts, cid))
        if cid:
            spawn_ts_by_id.setdefault(cid, ts)

    # Resolve launch timestamps in two deterministic passes instead of the
    # children list's (filename) order. Filename order does not represent
    # launch order, and the claimed-launch bookkeeping makes a single greedy
    # pass order-dependent: when both the Agent calls and the children lack
    # ids, whichever child is listed first claims the eligible launch and the
    # rest fall back to their own first record — assigning the wrong window
    # start and mis-counting spawns_parent_kept_working.
    top_children = [(i, c) for i, c in enumerate(children) if c.spawn_depth <= 1]
    scan_bounds: dict[int, tuple[float | None, float | None]] = {}
    for i, child in top_children:
        scanned = scan_records(child.records, harness)
        scan_bounds[i] = (scanned.first_ts, scanned.last_ts)

    spawn_ts_by_child: dict[int, float | None] = {}
    claimed_launches: set[int] = set()
    unmatched: list[tuple[int, ChildSpec]] = []
    # Pass 1: a child whose metadata carries an id claims that launch.
    for i, child in top_children:
        agent_id = child.session_id.removeprefix("agent-")
        matched_ts: float | None = None
        for key in (child.tool_use_id, agent_id, child.session_id):
            if not key:
                continue
            id_ts = spawn_ts_by_id.get(str(key))
            if id_ts is None:
                continue
            for j, (lts, lid) in enumerate(launches):
                if j not in claimed_launches and lid == str(key) and lts == id_ts:
                    claimed_launches.add(j)
                    break
            matched_ts = id_ts
            break
        if matched_ts is None:
            unmatched.append((i, child))
        else:
            spawn_ts_by_child[i] = matched_ts
    # Pass 2: remaining children take the latest unclaimed launch at or before
    # their first record, in ascending first-record order (a stable,
    # order-independent chronological assignment).
    for i, _child in sorted(
        unmatched,
        key=lambda item: (
            scan_bounds[item[0]][0] is None,
            scan_bounds[item[0]][0] or 0.0,
        ),
    ):
        first_ts = scan_bounds[i][0]
        best_j: int | None = None
        best_ts: float | None = None
        for j, (lts, _lid) in enumerate(launches):
            if j in claimed_launches:
                continue
            if first_ts is None or lts <= first_ts:
                if best_ts is None or lts >= best_ts:
                    best_ts = lts
                    best_j = j
        if best_j is not None and best_ts is not None:
            claimed_launches.add(best_j)
            spawn_ts_by_child[i] = best_ts
        else:
            spawn_ts_by_child[i] = first_ts

    kept = 0
    for i, child in top_children:
        agent_id = child.session_id.removeprefix("agent-")
        t_spawn = spawn_ts_by_child[i]
        # The child's last emitted record is its actual completion time; the
        # task-notification is delivered to the parent later, so preferring
        # notif_by_id here would extend the kept-working window past the
        # child's finish and over-count parent turns as kept-working.
        t_done = (
            child_done_ts.get(agent_id)
            or scan_bounds[i][1]
            or notif_by_id.get(agent_id)
            or (notif_by_id.get(str(child.tool_use_id)) if child.tool_use_id else None)
        )
        if t_spawn is None or t_done is None:
            continue
        parent_turns = sum(
            1
            for turn in parent.turn_ts
            if t_spawn < turn < t_done and turn not in parent.spawn_turn_ts
        )
        if parent_turns >= 1:
            kept += 1
    summary["spawns_parent_kept_working"] = kept

    idle_max = 0.0
    if intervals and len(parent.turn_ts) > 1:
        for start, end in zip(parent.turn_ts, parent.turn_ts[1:]):
            gap = end - start
            if gap > idle_max and any(a <= start and end <= b + 5 for a, b in intervals):
                idle_max = gap
    summary["parent_idle_max_seconds"] = int(round(idle_max)) if intervals else 0
    return summary


def summarize_session_tree(tree: Any) -> dict[str, Any]:
    """Compute ``subagent_summary`` from a :class:`SessionTree`."""
    harness = getattr(getattr(tree, "parent", None), "harness", None)
    return summarize_subagents(
        list(getattr(tree, "parent_records", []) or []),
        _walk_tree_children(tree),
        harness=harness,
    )
