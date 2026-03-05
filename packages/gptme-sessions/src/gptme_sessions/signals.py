"""Extract productivity signals from agent trajectory files.

Supports four trajectory formats:
- **gptme**: conversation.jsonl with top-level role/content/timestamp fields
- **Claude Code**: session .jsonl with top-level type/message/timestamp fields
- **Codex**: rollout-*.jsonl from ~/.codex/sessions/ with type/payload structure
- **Copilot**: events.jsonl from ~/.copilot/session-state/ with type/data structure

Provides grounded reward signals for Thompson sampling and session analytics
by analyzing actual transcripts rather than self-reported journals.

Journals cannot be relied on for consistent structure. Trajectories are
ground truth: every tool call, every error, every file write is recorded.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# Regex for git commit lines in shell output (works for both harnesses)
_COMMIT_RE = re.compile(r"\[(?:master|main|[a-zA-Z0-9_/-]+)\s+([0-9a-f]{7,12})\]\s+(.+?)(?:\n|$)")

# Tools that write files in Claude Code
# Note: gptme has a "patch" tool but Claude Code does not — no "Patch" here
_CC_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}


def parse_trajectory(jsonl_path: Path) -> list[dict]:
    """Parse a JSONL trajectory file into a list of records."""
    msgs = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return msgs


def _detect_format(msgs: list[dict]) -> str:
    """Detect trajectory format: 'claude_code', 'codex', 'copilot', or 'gptme'.

    - Claude Code: top-level 'type' in (user/assistant/result)
    - Codex: top-level 'type' in (session_meta/response_item/turn_context/event_msg)
    - Copilot: top-level 'type' matches 'session.*' or 'tool.execution_*'
    - gptme: top-level 'role' field (user/assistant/system)

    Scans all records to handle trajectories that begin with non-standard types.
    """
    for msg in msgs:
        if "role" in msg:
            return "gptme"
        t = msg.get("type", "")
        if t in ("user", "assistant", "result"):
            return "claude_code"
        if t in ("session_meta", "turn_context") or (
            t == "response_item" and isinstance(msg.get("payload"), dict)
        ):
            return "codex"
        if t.startswith("session.") or t.startswith("tool.execution") or t.startswith("assistant."):
            return "copilot"
    return "gptme"  # default


def _parse_timestamp(ts_str: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string, returning None on failure."""
    if not ts_str:
        return None
    try:
        from datetime import timezone as _tz

        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        return ts
    except ValueError:
        return None


def _extract_path_from_args(args_str: str) -> str | None:
    """Extract the 'path' field from a tool call's JSON args string."""
    m = re.search(r'"path"\s*:\s*"([^"]+)"', args_str)
    return m.group(1) if m else None


def extract_signals(msgs: list[dict]) -> dict:
    """Extract productivity signals from parsed gptme trajectory messages.

    Returns a dict with counts and extracted deliverables — no LLM needed.

    Tool call format in gptme trajectories:
      @tool_name(call_id): {"arg": "value", ...}
    Tool results are system messages with timestamps.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0  # number of assistant turns that yielded to await tool results

    # Track recent (tool, path) pairs for retry detection
    recent_sigs: list[str] = []

    for msg in msgs:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        ts_str = msg.get("timestamp", "")

        # Parse timestamps for duration
        if ts_str:
            ts = _parse_timestamp(ts_str)
            if ts is not None:
                timestamps.append(ts)

        if role == "assistant":
            # Find all @tool_name(call_id): JSON_ARGS blocks.
            blocks = re.split(r"(?=\n@\w+\()", "\n" + content)
            step_has_tool = False
            for block in blocks:
                m = re.match(r"\n@(\w+)\(([^)]+)\):\s*(.*)", block, re.DOTALL)
                if not m:
                    continue
                tool, _call_id, args_str = m.group(1), m.group(2), m.group(3)
                tool_calls[tool] = tool_calls.get(tool, 0) + 1
                step_has_tool = True

                if tool in ("save", "write", "patch", "edit"):
                    path = _extract_path_from_args(args_str)
                    if path:
                        if "/journal/" not in path:
                            file_writes.append(path)
                            sig = f"{tool}:{path}"
                            if sig in recent_sigs:
                                retry_candidates.append(tool)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 20:
                                recent_sigs.pop(0)
                    # No else: tool calls without extractable paths are not counted as
                    # file writes. Placeholder strings like "<save>" would inflate the
                    # unique-write count used in grade_signals and push unproductive
                    # sessions into higher reward tiers.
            if step_has_tool:
                steps += 1

        elif role == "system" and ts_str:
            content_stripped = content.strip()

            # Error detection (guard applies to all conditions, not just the last)
            if not content_stripped.startswith("Ran command:") and (
                content_stripped.startswith("Error")
                or "Error during execution:" in content[:100]
                or "error:" in content[:80].lower()
            ):
                error_count += 1

            # Git commit detection from shell output (finditer handles multi-commit pushes).
            # Restrict to first 500 bytes: real git output appears early; reading a file
            # that happens to contain git-log-style lines would produce false positives.
            for commit_match in _COMMIT_RE.finditer(content[:500]):
                commit_hash = commit_match.group(1)
                commit_msg = commit_match.group(2).strip()
                git_commits.append(f"{commit_msg} ({commit_hash})")

    # Session duration
    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    # Combine deliverables: git commits + distinct file writes
    deliverables = list(dict.fromkeys(git_commits + file_writes))
    retry_count = len(retry_candidates)

    return {
        "tool_calls": tool_calls,
        "steps": steps,
        "error_count": error_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "session_duration_s": duration_s,
        "retry_count": retry_count,
        "deliverables": deliverables,
    }


def grade_signals(signals: dict) -> float:
    """Compute a graded reward (0.0-1.0) from trajectory signals.

    Based on ground-truth evidence from the transcript:
    - Git commits (strongest signal)
    - File writes / patches
    - Error rate penalty
    - Retry penalty
    """
    commits = len(signals["git_commits"])
    # Use unique writes for tier placement — repeated edits to the same file
    # should not inflate the grade tier beyond what the retry penalty recovers.
    writes = len(set(signals["file_writes"]))
    errors = signals["error_count"]
    retries = signals["retry_count"]
    total_tools = sum(signals["tool_calls"].values())
    # gptme has a 'complete' tool that signals intentional session end (as opposed
    # to running out of context or timing out). Small bonus for structured completion.
    has_complete = "complete" in signals["tool_calls"]

    if commits == 0 and writes == 0:
        # Distinguish dead sessions (zero tool calls) from active-but-unproductive ones
        reward = 0.10 if total_tools == 0 else 0.25
    elif commits == 0:
        if writes >= 3:
            reward = 0.55
        else:
            reward = 0.40
    elif commits == 1:
        reward = 0.60
    elif commits == 2:
        reward = 0.70
    elif commits == 3:
        reward = 0.78
    else:
        reward = min(0.92, 0.80 + 0.03 * (commits - 4))

    if total_tools > 0:
        error_rate = errors / total_tools
        if error_rate > 0.15:
            reward -= 0.10
        elif error_rate > 0.05:
            reward -= 0.05

    if retries >= 3:
        reward -= 0.08
    elif retries >= 1:
        reward -= 0.03

    if has_complete:
        reward += 0.03

    return max(0.0, min(1.0, reward))


def is_productive(signals: dict) -> bool:
    """Quick binary productive/noop classification from trajectory signals."""
    return bool(signals["git_commits"] or len(set(signals["file_writes"])) >= 2)


def extract_signals_cc(msgs: list[dict]) -> dict:
    """Extract productivity signals from Claude Code .jsonl trajectories.

    CC format: each record has a top-level 'type' field.
    - type='assistant': message.content is a list; tool calls have type='tool_use'
    - type='user': message.content is a list; tool results have type='tool_result'
    - type='result': final session result record

    Tool names for file writes: Write, Edit (input.file_path), NotebookEdit (input.notebook_path).
    Errors: tool_result items with is_error=True.
    Git commits: detected from Bash tool output content via regex.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0  # number of assistant turns that yielded to await tool results
    recent_sigs: list[str] = []
    # Map tool_use id → tool name for filtering commit detection to Bash only
    tool_id_to_name: dict[str, str] = {}

    for record in msgs:
        rec_type = record.get("type", "")

        # Parse top-level timestamp (present on user/assistant/result records)
        ts = _parse_timestamp(record.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)

        if rec_type == "assistant":
            content = record.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            step_has_tool = False
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                tool = item.get("name", "")
                if not tool:
                    continue
                tool_calls[tool] = tool_calls.get(tool, 0) + 1
                step_has_tool = True

                # Track id → name for commit detection filtering
                tool_id = item.get("id", "")
                if tool_id:
                    tool_id_to_name[tool_id] = tool

                if tool in _CC_WRITE_TOOLS:
                    inp = item.get("input", {})
                    # NotebookEdit uses 'notebook_path'; all other write tools use 'file_path'
                    path = (
                        inp.get("notebook_path", "")
                        if tool == "NotebookEdit"
                        else inp.get("file_path", "")
                    )
                    if path:
                        if "/journal/" not in path:
                            file_writes.append(path)
                            sig = f"{tool}:{path}"
                            if sig in recent_sigs:
                                retry_candidates.append(tool)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 20:
                                recent_sigs.pop(0)
                    # No else: tool calls without extractable paths are not counted as
                    # file writes — same rationale as the gptme path above.
            if step_has_tool:
                steps += 1

        elif rec_type == "user":
            content = record.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue

                if item.get("is_error"):
                    error_count += 1
                    continue

                # Content can be a string or a list of content blocks
                result_content = item.get("content", "")
                if isinstance(result_content, list):
                    result_str = " ".join(
                        c.get("text", str(c)) if isinstance(c, dict) else str(c)
                        for c in result_content
                    )
                else:
                    result_str = str(result_content)

                # Git commit detection: only from Bash tool output.
                # Other tools (Read, Glob, Write) can return content containing
                # commit-like patterns from files, which would be false positives.
                tool_use_id = item.get("tool_use_id", "")
                if tool_id_to_name.get(tool_use_id) == "Bash":
                    for commit_match in _COMMIT_RE.finditer(result_str):
                        commit_hash = commit_match.group(1)
                        commit_msg = commit_match.group(2).strip()
                        git_commits.append(f"{commit_msg} ({commit_hash})")

    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    deliverables = list(dict.fromkeys(git_commits + file_writes))
    return {
        "tool_calls": tool_calls,
        "steps": steps,
        "error_count": error_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "session_duration_s": duration_s,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


def extract_usage_cc(msgs: list[dict]) -> dict:
    """Extract cumulative token usage from a Claude Code trajectory.

    Each CC assistant turn stores a `message.usage` snapshot from the Anthropic API.
    The fields represent per-API-call values (not cumulative context size):
      - input_tokens: freshly processed (non-cached) input tokens for that turn
      - cache_read_input_tokens: context served from the prompt cache
      - cache_creation_input_tokens: tokens written to the prompt cache
      - output_tokens: tokens generated by the model
    Summing across turns gives the true session totals for billing/cost purposes.

    Returns the last-seen model string alongside the totals.
    Returns an empty dict if no usage data is found (e.g., no assistant turns).

    This is the canonical way to get per-session token counts from CC trajectories,
    as opposed to parsing systemd journal output (which is fragile).
    """
    input_tokens = 0
    output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    model: str | None = None

    for record in msgs:
        if record.get("type") != "assistant":
            continue
        msg = record.get("message", {})
        usage = msg.get("usage") or {}
        input_tokens += usage.get("input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
        cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        if msg.get("model"):
            model = msg["model"]

    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total_tokens == 0 and model is None:
        # No assistant turns with usage data found
        return {}
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
    }


def extract_signals_codex(msgs: list[dict]) -> dict:
    """Extract productivity signals from Codex rollout-*.jsonl trajectories.

    Codex format: each record has top-level ``type``, ``timestamp``, and ``payload``.
    - ``session_meta``: session ID, CLI version, model_provider
    - ``turn_context``: ``payload.model`` — canonical model string for this turn
    - ``response_item`` with ``payload.type == "function_call"``: shell tool call
      (``exec_command``). Args are a JSON string in ``payload.arguments``.
    - ``response_item`` with ``payload.type == "function_call_output"``: shell result.
      ``payload.output`` is plain text; contains "Process exited with code N".
    - ``response_item`` with ``payload.type == "custom_tool_call"``: structured tool
      (e.g., ``apply_patch``). ``payload.input`` is the patch string.
    - ``response_item`` with ``payload.type == "custom_tool_call_output"``: JSON string
      with ``{"output": ..., "metadata": {"exit_code": N}}``.
    - ``event_msg`` with ``payload.type == "task_complete"``: session end marker.

    Git commits are detected from shell (exec_command) output text.
    File writes are detected from ``apply_patch`` custom tool calls.
    Errors are detected from non-zero exit codes in tool outputs.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0
    recent_sigs: list[str] = []
    # Map call_id → tool name so we scan commits only from shell outputs
    call_id_to_name: dict[str, str] = {}
    # Track whether the current turn (bounded by turn_context records) has tools.
    # Increment steps once per turn, not once per tool call/output pair.
    current_turn_has_tool = False

    for record in msgs:
        ts = _parse_timestamp(record.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)

        rec_type = record.get("type", "")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        p_type = payload.get("type", "")

        if rec_type == "turn_context":
            # New turn boundary: flush previous turn's step count
            if current_turn_has_tool:
                steps += 1
                current_turn_has_tool = False

        elif rec_type == "response_item":
            if p_type == "function_call":
                name = payload.get("name", "")
                if not name:
                    continue
                tool_calls[name] = tool_calls.get(name, 0) + 1
                current_turn_has_tool = True
                call_id = payload.get("call_id", "")
                if call_id:
                    call_id_to_name[call_id] = name

            elif p_type == "custom_tool_call":
                name = payload.get("name", "")
                if not name:
                    continue
                tool_calls[name] = tool_calls.get(name, 0) + 1
                current_turn_has_tool = True
                call_id = payload.get("call_id", "")
                if call_id:
                    call_id_to_name[call_id] = name

                # apply_patch writes files; extract path from patch header if possible
                if name == "apply_patch":
                    patch_text = payload.get("input", "")
                    # Patch headers look like: "*** Update File: /path/to/file"
                    path_match = re.search(r"\*\*\* (?:Update|Add) File: (.+)", str(patch_text))
                    if path_match:
                        path = path_match.group(1).strip()
                        if "/journal/" not in path:
                            file_writes.append(path)
                            sig = f"apply_patch:{path}"
                            if sig in recent_sigs:
                                retry_candidates.append(name)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 20:
                                recent_sigs.pop(0)

            elif p_type == "function_call_output":
                output = payload.get("output", "")
                call_id = payload.get("call_id", "")
                # Error detection: non-zero exit code in output text
                exit_match = re.search(r"Process exited with code (\d+)", output)
                if exit_match and exit_match.group(1) != "0":
                    error_count += 1
                # Git commit detection: only from exec_command (shell) outputs.
                # Codex wraps output as "Chunk ID:...\nOutput:\n<actual>".
                # Extract just the actual shell output to skip the preamble,
                # then search the full text (no 500-byte limit) — pre-commit
                # hooks can push the commit line thousands of bytes in.
                if call_id_to_name.get(call_id) == "exec_command":
                    out_match = re.search(r"\bOutput:\n(.*)", output, re.DOTALL)
                    out_str = out_match.group(1) if out_match else output
                    for commit_match in _COMMIT_RE.finditer(out_str):
                        git_commits.append(
                            f"{commit_match.group(2).strip()} ({commit_match.group(1)})"
                        )

            elif p_type == "custom_tool_call_output":
                output_raw = payload.get("output", "")
                try:
                    output_obj = json.loads(output_raw)
                    exit_code = (output_obj.get("metadata") or {}).get("exit_code", 0)
                    if exit_code and exit_code != 0:
                        error_count += 1
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

    # Flush final turn
    if current_turn_has_tool:
        steps += 1

    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    deliverables = list(dict.fromkeys(git_commits + file_writes))
    return {
        "tool_calls": tool_calls,
        "steps": steps,
        "error_count": error_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "session_duration_s": duration_s,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


def extract_signals_copilot(msgs: list[dict]) -> dict:
    """Extract productivity signals from GitHub Copilot events.jsonl trajectories.

    Copilot format: each record has top-level ``type``, ``id``, ``timestamp``,
    ``data``, and optionally ``parentId``.
    - ``session.start``: ``data.selectedModel``, ``data.startTime``
    - ``session.model_change``: ``data.newModel`` (model switch mid-session)
    - ``assistant.message``: ``data.toolRequests`` list of tool calls
    - ``tool.execution_start``: ``data.toolName``, ``data.arguments``
    - ``tool.execution_complete``: ``data.success``, ``data.result.content``

    Git commits are detected from ``tool.execution_complete`` results for ``bash`` calls.
    File writes are detected from ``tool.execution_start`` with ``toolName == "edit"``.
    Errors are detected from ``tool.execution_complete`` with ``success == false``.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0
    recent_sigs: list[str] = []
    # Map toolCallId → tool name for commit filtering
    call_id_to_name: dict[str, str] = {}
    current_turn_has_tool = False

    for record in msgs:
        ts = _parse_timestamp(record.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)

        rec_type = record.get("type", "")
        data = record.get("data", {})
        if not isinstance(data, dict):
            continue

        if rec_type == "tool.execution_start":
            name = data.get("toolName", "")
            if not name:
                continue
            tool_calls[name] = tool_calls.get(name, 0) + 1
            current_turn_has_tool = True
            call_id = data.get("toolCallId", "")
            if call_id:
                call_id_to_name[call_id] = name

            # File write detection for edit tool
            if name == "edit":
                args = data.get("arguments", {})
                path = args.get("path", "") if isinstance(args, dict) else ""
                if path and "/journal/" not in path:
                    file_writes.append(path)
                    sig = f"edit:{path}"
                    if sig in recent_sigs:
                        retry_candidates.append(name)
                    recent_sigs.append(sig)
                    if len(recent_sigs) > 20:
                        recent_sigs.pop(0)

        elif rec_type == "tool.execution_complete":
            if not data.get("success", True):
                error_count += 1
            # Git commit detection: only from bash tool results
            call_id = data.get("toolCallId", "")
            if call_id_to_name.get(call_id) == "bash":
                result = data.get("result", {})
                content = result.get("content", "") if isinstance(result, dict) else ""
                for commit_match in _COMMIT_RE.finditer(str(content)[:500]):
                    git_commits.append(f"{commit_match.group(2).strip()} ({commit_match.group(1)})")

        elif rec_type == "assistant.turn_end":
            if current_turn_has_tool:
                steps += 1
                current_turn_has_tool = False

    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    deliverables = list(dict.fromkeys(git_commits + file_writes))
    return {
        "tool_calls": tool_calls,
        "steps": steps,
        "error_count": error_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "session_duration_s": duration_s,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


def extract_from_path(jsonl_path: Path) -> dict:
    """Parse trajectory and return signals + grade in one call.

    Auto-detects format: gptme, claude_code, codex, or copilot.
    For CC format, token usage is also extracted from the trajectory.
    """
    msgs = parse_trajectory(jsonl_path)
    fmt = _detect_format(msgs)
    usage: dict = {}
    if fmt == "claude_code":
        signals = extract_signals_cc(msgs)
        usage = extract_usage_cc(msgs)
    elif fmt == "codex":
        signals = extract_signals_codex(msgs)
    elif fmt == "copilot":
        signals = extract_signals_copilot(msgs)
    else:
        signals = extract_signals(msgs)
    grade = grade_signals(signals)
    result: dict = {
        **signals,
        "format": fmt,
        "productive": is_productive(signals),
        "grade": round(grade, 4),
    }
    if usage:
        result["usage"] = usage
    return result
