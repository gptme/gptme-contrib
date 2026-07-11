"""Capture harness failure reason and error detail for session records."""

from __future__ import annotations

import json
import re
from pathlib import Path

# Coarse taxonomy for operator-pulse / bandit post-mortems.
FAILURE_REASON_AUTH = "auth"
FAILURE_REASON_NONZERO = "nonzero_exit_unclassified"
FAILURE_REASON_PRE_RESPONSE = "pre_response_api_failure"
FAILURE_REASON_RATE_LIMIT = "rate_limit"
FAILURE_REASON_TIMEOUT = "timeout"

_ERROR_LINE_RE = re.compile(
    r"(?i)(error|exception|traceback|failed|rate.?limit|401|403|429|authentication)"
)
_ASSISTANT_ROLES = frozenset({"assistant"})


def _read_tail(path: Path, *, max_bytes: int = 16_000, max_lines: int = 40) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    text = data[-max_bytes:].decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    tail = "\n".join(lines[-max_lines:])
    return tail[:4000] if len(tail) > 4000 else tail


def _record_is_assistant(rec: dict) -> bool:
    """Return True for flat (gptme) and nested (Claude Code) assistant records."""
    # Flat format: {"role": "assistant", "content": "..."}
    if rec.get("role") in _ASSISTANT_ROLES:
        return True
    # CC nested format: {"type": "assistant", "message": {"role": "assistant", ...}}
    if rec.get("type") == "assistant":
        msg = rec.get("message") or {}
        if msg.get("role") in _ASSISTANT_ROLES or rec.get("type") == "assistant":
            return True
    return False


def _record_content_text(rec: dict) -> str:
    """Extract flattened content string from flat or CC nested record."""
    # Flat format
    content = rec.get("content") or rec.get("text") or ""
    if content:
        return str(content)
    # CC nested: message.content is a list of typed blocks
    msg = rec.get("message") or {}
    msg_content = msg.get("content") or ""
    if isinstance(msg_content, list):
        parts = [block.get("text") or "" for block in msg_content if isinstance(block, dict)]
        return " ".join(p for p in parts if p)
    return str(msg_content)


def _trajectory_has_assistant(trajectory_path: Path | None) -> bool:
    if trajectory_path is None or not trajectory_path.is_file():
        return False
    try:
        with trajectory_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _record_is_assistant(rec) and _record_content_text(rec).strip():
                    return True
    except OSError:
        return False
    return False


def _extract_trajectory_error_line(trajectory_path: Path | None) -> str | None:
    if trajectory_path is None or not trajectory_path.is_file():
        return None
    last_match: str | None = None
    try:
        with trajectory_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = _record_content_text(rec)
                if not content:
                    continue
                for part in content.splitlines():
                    part = part.strip()
                    if part and _ERROR_LINE_RE.search(part):
                        last_match = part[:500]
    except OSError:
        return None
    return last_match


def classify_failure_reason(
    *,
    exit_code: int,
    duration_seconds: int,
    input_tokens: int | None,
    has_assistant_turn: bool,
    error_text: str | None,
) -> str:
    if exit_code == 124:
        return FAILURE_REASON_TIMEOUT
    if error_text:
        lower = error_text.lower()
        if ("rate" in lower and "limit" in lower) or "429" in error_text:
            return FAILURE_REASON_RATE_LIMIT
        if (
            "auth" in lower
            or "authentication" in lower
            or "401" in error_text
            or "403" in error_text
        ):
            return FAILURE_REASON_AUTH
    # has_assistant_turn is definitive: if the trajectory has a response, model ran.
    # input_tokens==0 alone can't override it — token accounting can be missing on CC.
    no_model_tokens = not has_assistant_turn and (input_tokens == 0 or input_tokens is None)
    if exit_code != 0 and no_model_tokens and duration_seconds < 120:
        return FAILURE_REASON_PRE_RESPONSE
    return FAILURE_REASON_NONZERO


def capture_session_failure(
    *,
    exit_code: int,
    duration_seconds: int,
    input_tokens: int | None,
    trajectory_path: Path | None,
    harness_stderr_path: Path | None,
) -> tuple[str | None, str | None]:
    """Return ``(failure_reason, error)`` for failed harness exits.

    ``None`` pair when ``exit_code`` is zero (success).
    """
    if exit_code == 0:
        return None, None

    has_assistant = _trajectory_has_assistant(trajectory_path)
    stderr_tail = _read_tail(harness_stderr_path) if harness_stderr_path else None
    traj_error = _extract_trajectory_error_line(trajectory_path)
    error_text = stderr_tail or traj_error
    if stderr_tail and traj_error and traj_error not in stderr_tail:
        error_text = f"{stderr_tail}\n---\n{traj_error}"

    reason = classify_failure_reason(
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        has_assistant_turn=has_assistant,
        error_text=error_text,
    )
    detail = error_text
    if detail is None and reason == FAILURE_REASON_PRE_RESPONSE:
        detail = "exit before first assistant response (input_tokens=0)"
    return reason, detail
