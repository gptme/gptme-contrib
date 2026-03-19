"""Extract productivity signals from agent trajectory files.

Supports four trajectory formats:
- **gptme**: conversation.jsonl with top-level role/content/timestamp fields
- **Claude Code**: session .jsonl with top-level type/message/timestamp fields
- **Codex CLI**: JSONL with typed entries (session_meta, turn_context, response_item, event_msg)
- **Copilot CLI**: JSONL with typed events (session.start, assistant.message, tool.*)

Provides grounded reward signals for Thompson sampling and session analytics
by analyzing actual transcripts rather than self-reported journals.

Journals cannot be relied on for consistent structure. Trajectories are
ground truth: every tool call, every error, every file write is recorded.
"""

from __future__ import annotations

import glob as _glob
import json
import os
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
    """Detect trajectory format from parsed JSONL records.

    Returns one of: 'gptme', 'claude_code', 'codex', 'copilot'.

    Detection heuristics (checked in order):
    - **Codex**: first record has type='session_meta' with originator='codex_exec'
    - **Copilot**: first record has type='session.start' with producer='copilot-agent'
    - **gptme**: any record has a top-level 'role' field
    - **Claude Code**: any record has type in (user, assistant, result)

    Scans all records (not just the first few) to handle CC trajectories that
    begin with non-standard record types like 'queue-operation' or 'system_prompt'.
    """
    if msgs:
        first = msgs[0]
        # Codex: first line is always session_meta
        if first.get("type") == "session_meta":
            payload = first.get("payload") or {}
            if payload.get("originator") in ("codex_exec", "codex_interactive"):
                return "codex"
        # Copilot: first line is always session.start
        if first.get("type") == "session.start":
            data = first.get("data") or {}
            if data.get("producer") == "copilot-agent":
                return "copilot"

    for msg in msgs:
        if "role" in msg:
            return "gptme"
        if msg.get("type") in ("user", "assistant", "result"):
            return "claude_code"
    return "gptme"  # default


def detect_format(msgs: list[dict]) -> str:
    """Public alias for trajectory format detection.

    Exposes format detection as stable API while preserving _detect_format for
    internal callers and backward compatibility.
    """
    return _detect_format(msgs)


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
    journal_paths: list[str] = []
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
                        else:
                            journal_paths.append(path)
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
        "journal_paths": list(dict.fromkeys(journal_paths)),
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
    warning_phrase_count = 0  # soft error signal: "error", "failed", etc. in output
    git_commits: list[str] = []
    file_writes: list[str] = []
    journal_paths: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0  # number of assistant turns that yielded to await tool results
    recent_sigs: list[str] = []
    # Map tool_use id → tool name for filtering commit detection to Bash only
    tool_id_to_name: dict[str, str] = {}
    # Per-tool-call timing: map tool_use_id → (tool_name, dispatch_timestamp)
    tool_dispatch: dict[str, tuple[str, datetime]] = {}
    tool_durations: dict[str, list[float]] = {}  # tool_name → list of durations (seconds)

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

                # Track id → name for commit detection filtering and timing
                tool_id = item.get("id", "")
                if tool_id:
                    tool_id_to_name[tool_id] = tool
                    # Record dispatch time for per-tool-call duration tracking
                    if ts is not None:
                        tool_dispatch[tool_id] = (tool, ts)

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
                        else:
                            journal_paths.append(path)
                    # No else: tool calls without extractable paths are not counted as
                    # file writes — same rationale as the gptme path above.
                elif tool == "Bash":
                    # Parse Bash commands for journal writes via cat heredoc redirects.
                    # Many CC sessions write journals via heredoc (cat > path << EOF)
                    # rather than the Write tool, so Write/Edit alone misses them.
                    # Note: tee redirects are not currently handled (intentional scope limit).
                    cmd = item.get("input", {}).get("command", "")
                    if "/journal/" in cmd:
                        for m in re.finditer(
                            r"cat\s*>>?\s*(.*?\.md)(?:\s+<<|\s*$)",
                            cmd,
                            re.MULTILINE,
                        ):
                            jpath = m.group(1).strip()
                            if "/journal/" not in jpath:
                                continue
                            # Resolve common shell date expansions using
                            # trajectory timestamps (more reliable than wall clock).
                            # Handle both $(date ...) and \$(date ...) (escaped in heredocs).
                            if "$(" in jpath and timestamps:
                                latest_ts = max(timestamps)
                                for pattern_str, replacement in [
                                    ("$(date +%Y-%m-%d)", latest_ts.strftime("%Y-%m-%d")),
                                    ("$(date +%H%M)", latest_ts.strftime("%H%M")),
                                ]:
                                    jpath = jpath.replace("\\" + pattern_str, replacement)
                                    jpath = jpath.replace(pattern_str, replacement)
                            # If unresolved ${VAR} remains, glob for a match.
                            # Sort by proximity to session end time (not recency)
                            # to avoid picking a different session's journal.
                            if "${" in jpath:
                                pattern = re.sub(r"\$\{[^}]+\}", "*", jpath)
                                session_end = max(timestamps).timestamp() if timestamps else 0
                                matches = sorted(
                                    _glob.glob(pattern),
                                    key=lambda p: abs(os.path.getmtime(p) - session_end)
                                    if os.path.exists(p)
                                    else float("inf"),
                                )
                                if matches:
                                    jpath = matches[0]
                                else:
                                    continue  # no match found
                            elif "$(" in jpath:
                                continue  # unresolved command substitution
                            # Only include paths that exist on disk — avoids false
                            # positives from debug/inspect commands that print templates
                            if not os.path.isfile(jpath):
                                continue
                            journal_paths.append(jpath)
            if step_has_tool:
                steps += 1

        elif rec_type == "user":
            content = record.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue

                tool_use_id = item.get("tool_use_id", "")

                # Per-tool-call duration: match result back to dispatch
                if tool_use_id in tool_dispatch and ts is not None:
                    dispatch_name, dispatch_ts = tool_dispatch[tool_use_id]
                    dur = (ts - dispatch_ts).total_seconds()
                    if dur >= 0:
                        tool_durations.setdefault(dispatch_name, []).append(dur)

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

                # Error/warning phrase detection in tool output (soft signal).
                # Elevated counts indicate problems even when is_error is false.
                # Only scan first 2000 chars to avoid false positives from large
                # reports or log files that naturally contain error strings.
                _scan = result_str[:2000].lower()
                for phrase in ("error:", "error ", "failed", "failure", "traceback", "exception"):
                    warning_phrase_count += _scan.count(phrase)

                # Git commit detection: only from Bash tool output.
                # Other tools (Read, Glob, Write) can return content containing
                # commit-like patterns from files, which would be false positives.
                if tool_id_to_name.get(tool_use_id) == "Bash":
                    for commit_match in _COMMIT_RE.finditer(result_str):
                        commit_hash = commit_match.group(1)
                        commit_msg = commit_match.group(2).strip()
                        git_commits.append(f"{commit_msg} ({commit_hash})")

    duration_s = 0
    if len(timestamps) >= 2:
        duration_s = int((max(timestamps) - min(timestamps)).total_seconds())

    deliverables = list(dict.fromkeys(git_commits + file_writes))

    # Summarize per-tool-call timing: total and max per tool name.
    # Enables detection of slow tests, long pre-commit hooks, and stalled tools.
    tool_time_total: dict[str, float] = {}
    tool_time_max: dict[str, float] = {}
    for name, durs in tool_durations.items():
        tool_time_total[name] = round(sum(durs), 1)
        tool_time_max[name] = round(max(durs), 1)
    total_tool_time_s = round(sum(tool_time_total.values()), 1)

    return {
        "tool_calls": tool_calls,
        "steps": steps,
        "error_count": error_count,
        "warning_phrase_count": warning_phrase_count,
        "git_commits": git_commits,
        "file_writes": file_writes,
        "journal_paths": list(dict.fromkeys(journal_paths)),
        "session_duration_s": duration_s,
        "total_tool_time_s": total_tool_time_s,
        "tool_time_total": tool_time_total,
        "tool_time_max": tool_time_max,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


def extract_usage_gptme(msgs: list[dict]) -> dict:
    """Extract cumulative token usage from a gptme conversation.jsonl trajectory.

    Reads token/cost data from msg.metadata, consistent with how gptme core
    tracks costs in gptme.util.cost_display.gather_conversation_costs().

    Only accumulates tokens from assistant turns (consistent with extract_usage_cc).
    Records the last-seen model (consistent with extract_usage_cc).

    Returns an empty dict if no usage data is found.
    """
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    cost = 0.0
    model: str | None = None

    for msg in msgs:
        if msg.get("role") != "assistant":
            continue

        metadata = msg.get("metadata") or {}
        if not metadata:
            continue

        # Last-seen model wins (consistent with extract_usage_cc)
        m = metadata.get("model") or msg.get("model")
        if m:
            model = m

        # Support both nested format (usage sub-dict) and legacy flat format
        # Select source dict once per message to avoid per-field falsy fallback issues
        usage = metadata.get("usage") or metadata
        input_tokens += usage.get("input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
        cache_read_tokens += usage.get("cache_read_tokens", 0)
        cache_creation_tokens += usage.get("cache_creation_tokens", 0)
        cost += metadata.get("cost", 0.0)

    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
    if total_tokens == 0 and cost == 0.0:
        return {}
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cost": cost,
        "total_tokens": total_tokens,
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
    """Extract productivity signals from Codex CLI .jsonl trajectories.

    Codex format uses typed entries:
    - session_meta: session start metadata
    - turn_context: per-turn context (model, cwd)
    - response_item: messages and tool calls (function_call / function_call_output)
    - event_msg: events (token_count, task lifecycle)

    Codex uses exec_command for all tool calls (shell-based). File writes are
    detected from shell redirect patterns rather than individual tool names
    since all operations go through the shell.
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    journal_paths: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0
    recent_sigs: list[str] = []

    # Track function_call ids for matching with their outputs
    call_id_to_name: dict[str, str] = {}

    # Steps count turns (bounded by turn_context records), not individual tool calls.
    # When no turn_context is present, all function_calls fall into one implicit turn.
    current_turn_has_tool = False

    for record in msgs:
        rec_type = record.get("type", "")

        ts = _parse_timestamp(record.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)

        if rec_type == "turn_context":
            # New turn boundary: finalize the previous turn's step count
            if current_turn_has_tool:
                steps += 1
            current_turn_has_tool = False

        elif rec_type == "response_item":
            payload = record.get("payload") or {}
            payload_type = payload.get("type", "")

            if payload_type == "function_call":
                tool = payload.get("name", "")
                if tool:
                    tool_calls[tool] = tool_calls.get(tool, 0) + 1
                    current_turn_has_tool = True
                    call_id = payload.get("call_id", "")
                    if call_id:
                        call_id_to_name[call_id] = tool

                    # Detect file writes from exec_command arguments
                    if tool == "exec_command":
                        args = payload.get("arguments", "")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                pass
                        if isinstance(args, dict):
                            cmd = args.get("cmd") or ""
                            # Detect redirects and file-writing commands.
                            # >(?!=) excludes >= comparison operators in heredoc content.
                            for write_match in re.finditer(
                                r"(?:cat\s*>\s*|tee\s+(?:-\S+\s+)*|>(?!=)\s*)([^\s<>|&;]+)",
                                cmd,
                            ):
                                path = write_match.group(1).strip("'\"")
                                if path.startswith("/dev/"):
                                    continue
                                elif "/journal/" in path:
                                    journal_paths.append(path)
                                else:
                                    file_writes.append(path)
                                    sig = f"exec_command:{path}"
                                    if sig in recent_sigs:
                                        retry_candidates.append("exec_command")
                                    recent_sigs.append(sig)
                                    if len(recent_sigs) > 20:
                                        recent_sigs.pop(0)

            elif payload_type == "function_call_output":
                output = payload.get("output") or ""
                call_id = payload.get("call_id", "")
                tool_name = call_id_to_name.get(call_id, "")

                # Error detection from shell output
                if "Process exited with code " in output:
                    code_match = re.search(r"Process exited with code (\d+)", output)
                    if code_match and code_match.group(1) != "0":
                        error_count += 1

                # Git commit detection from exec_command output
                if tool_name == "exec_command":
                    for commit_match in _COMMIT_RE.finditer(output[:500]):
                        commit_hash = commit_match.group(1)
                        commit_msg = commit_match.group(2).strip()
                        git_commits.append(f"{commit_msg} ({commit_hash})")

    # Finalize the last turn (not followed by another turn_context)
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
        "journal_paths": list(dict.fromkeys(journal_paths)),
        "session_duration_s": duration_s,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


# Tools that write files in Copilot CLI (subset of CC tools, lowercase)
_COPILOT_WRITE_TOOLS = {"edit", "write", "create"}


def extract_signals_copilot(msgs: list[dict]) -> dict:
    """Extract productivity signals from Copilot CLI events.jsonl trajectories.

    Copilot format uses typed events:
    - session.start: session metadata (model, cwd, git context)
    - assistant.message: agent responses with toolRequests[]
    - tool.execution_complete: tool results with success flag
    - assistant.turn_start/turn_end: turn boundaries

    Tool names are lowercase (bash, edit, view, glob, grep).
    """
    tool_calls: dict[str, int] = {}
    error_count = 0
    git_commits: list[str] = []
    file_writes: list[str] = []
    journal_paths: list[str] = []
    retry_candidates: list[str] = []
    timestamps: list[datetime] = []
    steps = 0
    recent_sigs: list[str] = []

    # Map toolCallId → tool name for filtering commit detection to bash only
    call_id_to_name: dict[str, str] = {}

    for record in msgs:
        rec_type = record.get("type", "")

        ts = _parse_timestamp(record.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)

        if rec_type == "assistant.message":
            data = record.get("data") or {}
            tool_requests = data.get("toolRequests") or []
            step_has_tool = False

            for req in tool_requests:
                tool = req.get("name", "")
                if not tool:
                    continue
                tool_calls[tool] = tool_calls.get(tool, 0) + 1
                step_has_tool = True

                call_id = req.get("toolCallId", "")
                if call_id:
                    call_id_to_name[call_id] = tool

                # File write detection from edit/write tools
                if tool in _COPILOT_WRITE_TOOLS:
                    args = req.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {}
                    if isinstance(args, dict):
                        path = args.get("path", "") or args.get("file_path", "")
                    else:
                        path = ""
                    if path:
                        if "/journal/" not in path:
                            file_writes.append(path)
                            sig = f"{tool}:{path}"
                            if sig in recent_sigs:
                                retry_candidates.append(tool)
                            recent_sigs.append(sig)
                            if len(recent_sigs) > 20:
                                recent_sigs.pop(0)
                        else:
                            journal_paths.append(path)

            if step_has_tool:
                steps += 1

        elif rec_type == "tool.execution_complete":
            data = record.get("data") or {}

            # Error detection from tool failure
            if data.get("success") is False:
                error_count += 1

            # Git commit detection from bash tool output
            call_id = data.get("toolCallId", "")
            tool_name = call_id_to_name.get(call_id, "")
            if tool_name == "bash":
                result = data.get("result") or {}
                content = result.get("detailedContent", "") or result.get("content", "")
                if isinstance(content, str):
                    for commit_match in _COMMIT_RE.finditer(content[:500]):
                        commit_hash = commit_match.group(1)
                        commit_msg = commit_match.group(2).strip()
                        git_commits.append(f"{commit_msg} ({commit_hash})")

        elif rec_type == "session.error":
            error_count += 1

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
        "journal_paths": list(dict.fromkeys(journal_paths)),
        "session_duration_s": duration_s,
        "retry_count": len(retry_candidates),
        "deliverables": deliverables,
    }


def extract_usage_codex(msgs: list[dict]) -> dict:
    """Extract model and rate-limit info from Codex CLI trajectories.

    Codex only provides rate-limit percentages (not absolute token counts).
    We extract the model name and last-seen rate limit usage for reference.

    Returns an empty dict if no model/usage data is found.
    """
    model: str | None = None
    rate_limit_primary: float | None = None
    rate_limit_secondary: float | None = None

    for record in msgs:
        rec_type = record.get("type", "")

        if rec_type == "turn_context":
            m = (record.get("payload") or {}).get("model")
            if m:
                model = m

        elif rec_type == "event_msg":
            payload = record.get("payload") or {}
            if payload.get("type") == "token_count":
                rl = payload.get("rate_limits") or {}
                primary = rl.get("primary") or {}
                secondary = rl.get("secondary") or {}
                if primary.get("used_percent") is not None:
                    rate_limit_primary = primary["used_percent"]
                if secondary.get("used_percent") is not None:
                    rate_limit_secondary = secondary["used_percent"]

    if model is None:
        return {}
    result: dict = {"model": model}
    if rate_limit_primary is not None:
        result["rate_limit_primary_pct"] = rate_limit_primary
    if rate_limit_secondary is not None:
        result["rate_limit_secondary_pct"] = rate_limit_secondary
    return result


def _extract_committed_files(git_commits: list[str]) -> list[str]:
    """Extract file paths changed in commits by running git diff-tree.

    Parses commit SHAs from the git_commits list (format: "message (sha)")
    and returns all unique file paths changed across those commits.

    For SHAs that don't resolve in the current repo, tries common submodule
    directories as fallback (cross-repo commits from submodules).

    Returns empty list if git is unavailable or SHAs can't be resolved.
    """
    import subprocess

    shas = []
    for entry in git_commits:
        # Extract SHA from "commit message (abc1234)" format
        m = re.search(r"\(([0-9a-f]{7,40})\)\s*$", entry)
        if m:
            shas.append(m.group(1))

    if not shas:
        return []

    # Discover candidate repos: submodules + sibling git repos in ../
    candidate_repos: list[str] = []

    # Submodules
    try:
        result = subprocess.run(
            ["git", "submodule", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.strip().lstrip("+-U").split()
                if len(parts) >= 2:
                    candidate_repos.append(parts[1])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Sibling directories that are git repos (../*)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            from pathlib import Path

            workspace_root = Path(result.stdout.strip())
            parent = workspace_root.parent
            for sibling in parent.iterdir():
                if sibling == workspace_root or not sibling.is_dir():
                    continue
                if (sibling / ".git").exists():
                    candidate_repos.append(str(sibling))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    def _diff_tree(sha: str, cwd: str | None = None) -> list[str]:
        try:
            cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, cwd=cwd)
            if result.returncode == 0:
                return [line for line in result.stdout.strip().splitlines() if line]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    files: list[str] = []
    for sha in shas:
        found = _diff_tree(sha)
        if not found:
            for repo_path in candidate_repos:
                found = _diff_tree(sha, cwd=repo_path)
                if found:
                    # Prefix with repo name for context
                    from pathlib import Path as _Path

                    repo_name = _Path(repo_path).name
                    found = [f"{repo_name}/{f}" for f in found]
                    break
        files.extend(found)
    return files


def infer_category(signals: dict) -> str | None:
    """Infer work category from commit messages, committed files, and file writes.

    Uses three signal sources:
    - Conventional commit prefixes and scopes from commit messages
    - File paths changed in commits (via git diff-tree)
    - File paths written by tools during the session

    Returns a category string or None if insufficient signal (< 2 votes).
    """
    commits = signals.get("git_commits", [])
    file_writes = signals.get("file_writes", [])

    # Get actual files changed in commits
    committed_files = _extract_committed_files(commits)

    # (prefix, scope) pairs where the prefix vote is suppressed — the prefix
    # is misleading for these. e.g. "docs(journal)" isn't content work,
    # "chore(submodule)" isn't meaningful. But "feat(sessions)" IS real work.
    _SUPPRESS_PREFIX_PAIRS = {
        ("docs", "journal"),
    }

    # Count commit prefix signals (skip when prefix+scope pair is suppressed)
    prefix_counts: dict[str, int] = {}
    for commit in commits:
        m = re.match(r"(\w+)\(([^)]+)\)", commit)
        if m and (m.group(1).lower(), m.group(2).lower()) in _SUPPRESS_PREFIX_PAIRS:
            continue
        # Extract conventional commit prefix: "type(scope): msg (sha)"
        m = re.match(r"(feat|fix|refactor|perf|test|ci|build|docs|style)\b", commit)
        if m:
            prefix_counts[m.group(1)] = prefix_counts.get(m.group(1), 0) + 1

    # Count scope signals from commits (suppressed scopes are implicitly excluded
    # because they have no entry in scope_to_category, not by explicit loop filtering)
    scope_counts: dict[str, int] = {}
    for commit in commits:
        m = re.match(r"\w+\(([^)]+)\)", commit)
        if m:
            scope = m.group(1).lower()
            scope_counts[scope] = scope_counts.get(scope, 0) + 1

    def _dir_in_path(segment: str, p: str) -> bool:
        """True if `segment` appears as a directory component in path `p`."""
        return bool(re.search(r"(^|/)" + re.escape(segment) + r"(/|$)", p))

    # Path-based signals from tool writes + committed files
    # Committed files are ground truth — weight them higher than tool writes
    all_paths: list[tuple[str, int]] = []
    for p in file_writes:
        all_paths.append((p, 1))
    for p in committed_files:
        all_paths.append((p, 2))  # committed files are stronger signal

    path_signals: dict[str, int] = {}
    for path, weight in all_paths:
        p = path.lower()
        if _dir_in_path("lesson", p) or _dir_in_path("lessons", p) or _dir_in_path("patterns", p):
            path_signals["knowledge"] = path_signals.get("knowledge", 0) + weight
        elif _dir_in_path("test", p) or _dir_in_path("tests", p) or "_test." in p or "test_" in p:
            path_signals["code"] = path_signals.get("code", 0) + weight
        elif _dir_in_path("scripts", p) or _dir_in_path("hooks", p) or _dir_in_path("ci", p):
            path_signals["infrastructure"] = path_signals.get("infrastructure", 0) + weight
        elif _dir_in_path("standup", p) or _dir_in_path("standups", p):
            path_signals["coordination"] = path_signals.get("coordination", 0) + weight
        elif _dir_in_path("tasks", p):
            path_signals["triage"] = path_signals.get("triage", 0) + weight
        elif p.endswith((".py", ".sh", ".ts", ".js", ".rs", ".go")):
            path_signals["code"] = path_signals.get("code", 0) + weight

    # Map commit prefixes → categories
    prefix_to_category = {
        "feat": "code",
        "fix": "code",
        "refactor": "code",
        "perf": "code",
        "test": "code",
        "ci": "infrastructure",
        "build": "infrastructure",
        "docs": "content",
        "style": "code",
    }

    # Scope overrides: certain scopes imply category regardless of prefix
    scope_to_category = {
        "lessons": "knowledge",
        "lesson": "knowledge",
        "tasks": "triage",
        "standup": "coordination",
        "orchestration": "coordination",
        "scripts": "infrastructure",
        "hooks": "infrastructure",
        "ci": "infrastructure",
    }

    # Tally votes
    votes: dict[str, int] = {}
    for prefix, count in prefix_counts.items():
        cat = prefix_to_category.get(prefix)
        if cat:
            votes[cat] = votes.get(cat, 0) + count
    for scope, count in scope_counts.items():
        cat = scope_to_category.get(scope)
        if cat:
            # Scope signals are stronger than prefix alone
            votes[cat] = votes.get(cat, 0) + count * 2
    for cat, count in path_signals.items():
        votes[cat] = votes.get(cat, 0) + count

    if not votes:
        return None

    best = max(votes, key=lambda k: votes[k])
    # Require at least 2 votes for confident classification
    if votes[best] < 2:
        return None
    return best


def extract_usage_copilot(msgs: list[dict]) -> dict:
    """Extract model info from Copilot CLI events.jsonl trajectories.

    Copilot events do not include per-turn token counts or cost data.
    We extract the model name from ``session.model_change`` events (or
    from ``session.start`` context if available) so callers always get a
    consistent ``{"model": ...}`` dict across all harnesses.

    Returns an empty dict if no model information is found.
    """
    model: str | None = None

    for record in msgs:
        rec_type = record.get("type", "")
        if rec_type == "session.model_change":
            m = (record.get("data") or {}).get("newModel")
            if m:
                model = m
        elif rec_type == "session.start" and model is None:
            # Some versions may include model in start context
            data = record.get("data") or {}
            m = data.get("selectedModel")
            if m:
                model = m

    if model is None:
        return {}
    return {"model": model}


def extract_from_path(jsonl_path: Path) -> dict:
    """Parse trajectory and return signals + grade in one call.

    Auto-detects format: gptme, Claude Code, Codex, or Copilot.
    Token usage is extracted when available (CC has full counts, Codex has
    rate-limit percentages only, Copilot has model info only).
    """
    msgs = parse_trajectory(jsonl_path)
    fmt = detect_format(msgs)
    if fmt == "claude_code":
        signals = extract_signals_cc(msgs)
        usage = extract_usage_cc(msgs)
    elif fmt == "codex":
        signals = extract_signals_codex(msgs)
        usage = extract_usage_codex(msgs)
    elif fmt == "copilot":
        signals = extract_signals_copilot(msgs)
        usage = extract_usage_copilot(msgs)
    else:
        signals = extract_signals(msgs)
        usage = extract_usage_gptme(msgs)
    grade = grade_signals(signals)
    result: dict = {
        **signals,
        "format": fmt,
        "productive": is_productive(signals),
        "grade": round(grade, 4),
        "inferred_category": infer_category(signals),
    }
    if usage:
        result["usage"] = usage
    return result
