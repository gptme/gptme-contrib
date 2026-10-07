"""One-shot, resumable backend runs: prompt in, final message + session id out.

:meth:`gptme_runloops.utils.executor.Executor.run_once` dispatches here. Unlike
``Executor.execute`` (the run-loop surface: stream output, return an exit code),
a one-shot run captures the agent's final message, the model it used and the
backend's own session id, and can resume an earlier session:

============  ==================================  ================================
backend       resume                              session id source
============  ==================================  ================================
claude-code   ``claude -p --resume <id>``         ``--output-format json``
codex         ``codex exec resume <id>``          ``thread.started`` JSONL event
gptme         ``gptme --name <id>``               generated conversation name
                                                  (reported only if persisted)
grok-build    not supported                       n/a
============  ==================================  ================================

Rules:

- Resume on a backend that cannot resume raises :class:`ResumeNotSupportedError`;
  it never silently starts fresh.
- A resume the backend rejects (unknown/expired id) returns an error result.
  Falling back to a fresh run is an explicit caller decision (``run`` CLI:
  ``--on-resume-failure fresh``).
- Tool restriction is backend-native: ``allowed_tools`` maps to claude-code
  ``--allowedTools`` (with ``--permission-mode acceptEdits`` instead of skipping
  permissions) and gptme ``--tools``. Codex has no allowlist, so ``allowed_tools``
  raises ``ValueError`` there; use ``sandbox`` (``read-only`` / ``workspace-write`` /
  ``danger-full-access``, default ``workspace-write``) instead.
- ``model`` is the model the backend reports having used (claude-code
  ``modelUsage``, gptme message metadata), else the requested one, else ``None``
  (backend default). Pass it back with ``resume`` to stay on the same model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_DEFAULT_SANDBOX = "workspace-write"


class ResumeNotSupportedError(ValueError):
    """Raised when resuming a session on a backend that cannot resume."""


@dataclass
class RunOnceResult:
    """Result of a one-shot run."""

    backend: str
    model: str | None
    session_id: str | None  # pass back as ``resume`` to continue this session
    result: str  # final agent message, or error text
    exit_code: int
    resumed: bool = False
    timed_out: bool = False
    cost_usd: float | None = None
    # False when the backend produced no agent message at all (e.g. it rejected
    # the resume id). Callers use it to tell "session never ran" from "ran and
    # reported an error", so a fallback never re-runs a session's side effects.
    agent_output: bool = True

    @property
    def is_error(self) -> bool:
        return self.exit_code != 0 or self.timed_out

    def to_dict(self) -> dict:
        return {**asdict(self), "is_error": self.is_error}


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a nested agent CLI: drop Claude Code nesting markers."""
    run_env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CC_SESSION_ID", "CC_MODEL"):
        run_env.pop(key, None)
    if env:
        run_env.update(env)
    return run_env


def _run(
    cmd: list[str], workspace: Path, timeout: int, env: dict[str, str] | None
) -> subprocess.CompletedProcess[str] | None:
    """Run capturing output; ``None`` on timeout."""
    try:
        return subprocess.run(
            cmd,
            cwd=workspace,
            env=clean_env(env),
            timeout=timeout,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None


def _timeout_result(
    backend: str,
    model: str | None,
    session: str | None,
    resume: str | None,
    timeout: int,
) -> RunOnceResult:
    return RunOnceResult(
        backend=backend,
        model=model,
        session_id=session,
        result=f"Timed out after {timeout}s.",
        exit_code=124,
        resumed=bool(resume),
        timed_out=True,
        agent_output=False,
    )


# --- claude-code ---


def _claude_main_model(model_usage: dict[str, Any]) -> str | None:
    """Pick the model that did most of the work (subagents may use others)."""
    if not model_usage:
        return None
    return max(
        model_usage,
        key=lambda m: (model_usage[m] or {}).get("outputTokens", 0),
    )


def claude_code_run_once(
    prompt: str,
    workspace: Path,
    timeout: int,
    *,
    model: str | None = None,
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    sandbox: str | None = None,
    env: dict[str, str] | None = None,
) -> RunOnceResult:
    if sandbox:
        raise ValueError("claude-code: sandbox is not supported; use allowed_tools")
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if resume:
        cmd += ["--resume", resume]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", *allowed_tools]
    else:
        cmd += ["--dangerously-skip-permissions"]
    r = _run(cmd, workspace, timeout, env)
    if r is None:
        return _timeout_result("claude-code", model, resume, resume, timeout)
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    text = out.get("result") or ""
    failed = r.returncode != 0 or bool(out.get("is_error")) or not text
    return RunOnceResult(
        backend="claude-code",
        model=_claude_main_model(out.get("modelUsage") or {}) or model,
        session_id=out.get("session_id") or resume,
        result=text or r.stderr[-2000:] or r.stdout[-2000:],
        exit_code=(r.returncode or 1) if failed else 0,
        resumed=bool(resume),
        cost_usd=out.get("total_cost_usd"),
        agent_output=bool(text),
    )


# --- codex ---


def codex_default_model() -> str | None:
    """Model codex uses when none is given (top-level ``model`` in its config)."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover
            import tomli as tomllib
        model = tomllib.loads((home / "config.toml").read_text()).get("model")
    except (OSError, ValueError):
        return None
    return model if isinstance(model, str) else None


def codex_run_once(
    prompt: str,
    workspace: Path,
    timeout: int,
    *,
    model: str | None = None,
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    sandbox: str | None = None,
    env: dict[str, str] | None = None,
) -> RunOnceResult:
    if allowed_tools:
        raise ValueError("codex: no tool allowlist; restrict with sandbox instead")
    sandbox = sandbox or CODEX_DEFAULT_SANDBOX
    if sandbox not in CODEX_SANDBOXES:
        raise ValueError(f"codex: unknown sandbox {sandbox!r}")
    with tempfile.TemporaryDirectory(prefix="codex-run-once-") as tmp:
        last = Path(tmp) / "last-message.txt"
        if resume:
            # `exec resume` has no --sandbox/-C flags; set sandbox via config.
            cmd = ["codex", "exec", "resume", "--json", "-o", str(last)]
            cmd += ["-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd = ["codex", "exec", "--json", "-o", str(last), "-s", sandbox]
            cmd += ["-C", str(workspace)]
        if model:
            cmd += ["-m", model]
        cmd += [resume, prompt] if resume else [prompt]
        r = _run(cmd, workspace, timeout, env)
        if r is None:
            return _timeout_result("codex", model, resume, resume, timeout)
        session, err = resume, None
        for line in r.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "thread.started":
                session = ev.get("thread_id") or session
            elif ev.get("type") in ("error", "turn.failed"):
                err = json.dumps(ev.get("error") or ev.get("message") or ev)[:2000]
        text = last.read_text().strip() if last.exists() else ""
    failed = r.returncode != 0 or not text
    return RunOnceResult(
        backend="codex",
        # codex JSONL does not report the model; fall back to its configured default
        model=model or codex_default_model(),
        session_id=session,
        result=text or err or r.stderr[-2000:],
        exit_code=(r.returncode or 1) if failed else 0,
        resumed=bool(resume),
        cost_usd=None,  # codex reports tokens, not cost
        agent_output=bool(text),
    )


# --- gptme ---


def gptme_logs_home() -> Path:
    if os.environ.get("GPTME_LOGS_HOME"):
        return Path(os.environ["GPTME_LOGS_HOME"])
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "gptme" / "logs"


# gptme tool names whose markdown-format calls (```<tool> ...```) are not prose.
GPTME_TOOL_BLOCKS = frozenset(
    "append browser chats choice complete computer elicit form gh ipython lessons "
    "patch precommit rag read save screenshot shell subagent tmux todo todoread "
    "todowrite vision".split()
)


def gptme_visible_text(content: str, tools: list[str] | None = None) -> str:
    """Strip reasoning and tool calls (``tool`` and markdown formats) from a
    gptme assistant message, leaving the prose shown to a human."""
    names = GPTME_TOOL_BLOCKS | set(tools or ())
    content = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", content, flags=re.S)

    def _strip_tool_block(m: re.Match[str]) -> str:
        return "" if m.group(1) in names else m.group(0)

    content = re.sub(
        r"^```([\w.-]+)[^\n]*\n.*?^```[ \t]*$",
        _strip_tool_block,
        content,
        flags=re.S | re.M,
    )
    lines = [ln for ln in content.splitlines() if not re.match(r"^@[\w-]+\(", ln)]
    return "\n".join(lines).strip()


def gptme_run_once(
    prompt: str,
    workspace: Path,
    timeout: int,
    *,
    model: str | None = None,
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    sandbox: str | None = None,
    env: dict[str, str] | None = None,
) -> RunOnceResult:
    if sandbox:
        raise ValueError("gptme: sandbox is not supported; use allowed_tools")
    # Unlike execute_gptme(), logs stay in the regular gptme logs home so the
    # named conversation can be resumed later.
    name = resume or f"run-once-{uuid.uuid4().hex[:12]}"
    cmd = ["gptme", "--non-interactive", "--output-format", "json", "--name", name]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--tools", ",".join(allowed_tools)]
    cmd += ["--workspace", str(workspace), prompt]
    r = _run(cmd, workspace, timeout, env)
    if r is None:
        return _timeout_result("gptme", model, name, resume, timeout)
    text, cost, used_model = "", 0.0, model
    for line in r.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "message" or msg.get("role") != "assistant":
            continue
        meta = msg.get("metadata") or {}
        cost += meta.get("cost") or 0.0
        used_model = meta.get("model") or used_model
        visible = gptme_visible_text(msg.get("content", ""), allowed_tools)
        if visible:
            text = visible
    failed = r.returncode != 0 or not text
    # Only report a session id gptme actually persisted under that name.
    session = name if (gptme_logs_home() / name).is_dir() else None
    return RunOnceResult(
        backend="gptme",
        model=used_model,
        session_id=session,
        result=text or r.stderr[-2000:],
        exit_code=(r.returncode or 1) if failed else 0,
        resumed=bool(resume),
        cost_usd=cost or None,
        agent_output=bool(text),
    )


RunOnceFn = Callable[..., RunOnceResult]

RUN_ONCE: dict[str, RunOnceFn] = {
    "claude-code": claude_code_run_once,
    "codex": codex_run_once,
    "gptme": gptme_run_once,
}
