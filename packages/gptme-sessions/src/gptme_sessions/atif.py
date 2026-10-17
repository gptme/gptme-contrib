"""ATIF v1.7 export adapter for gptme-sessions.

Converts a cross-harness SessionTranscript into the Agent Trajectory
Interchange Format (ATIF) v1.7 used by Pier and cross-harness RL pipelines
(DeepSWE, Terminal-Bench).

Schema reference: https://github.com/pier-ai/pier (commit 0c802fc, datacurve-pier 0.3.1)
See also: the `benchmark/pier-gptme/` offline smoke for a raw-message reference
implementation of the same grouping and linking rules.

## Conversion limits

- Tool call grouping: consecutive tool-call NormalizedMessages from the same
  assistant turn are merged onto one ATIF step. Text + tools from the same turn
  are merged when the text comes immediately before the tool calls.
- Call ID linking: tool_call_id is authoritative; unlinked results (no call ID)
  attach implicitly to the most recent agent step.
- Metrics: not available in NormalizedMessage (per-step usage is not in the
  normalized transcript format). Callers with access to raw trajectory records
  may set extra_steps_metadata to inject them.
- Images, attachment payloads, subagent trees, and reasoning deltas are omitted.
- An empty-message agent step is valid per ATIF when it carries tool_calls.
"""

from __future__ import annotations

import json
from typing import Any

from .transcript import SessionTranscript


def _new_agent_step(step_id: int, content: str, timestamp: str | None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "source": "agent", "message": content}
    if timestamp:
        step["timestamp"] = timestamp
    return step


def session_transcript_to_atif(
    transcript: SessionTranscript,
    *,
    notes: str | None = None,
    extra_agent_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a SessionTranscript to an ATIF v1.7 dict.

    Parameters
    ----------
    transcript:
        Normalized cross-harness transcript from ``read_transcript()``.
    notes:
        Optional human-readable annotation appended to the ATIF envelope.
    extra_agent_fields:
        Optional extra fields merged into the ``agent`` block (e.g.
        ``{"tool_format": "native"}`` for harness-specific metadata).

    Returns
    -------
    dict
        ATIF v1.7 envelope ready for ``json.dumps``.
    """
    steps: list[dict[str, Any]] = []
    # Maps tool_call_id → the ATIF step dict that owns that call.
    call_steps: dict[str, dict[str, Any]] = {}
    # The step (pending or already emitted) that accepts the next unlinked tool
    # result. Cleared by an explicit call-ID result or a user/system boundary.
    # Using a dedicated pointer rather than (pending or active_action) prevents
    # a post-explicit-result unlinked message from accidentally landing on the
    # still-pending step.
    implicit_target: dict[str, Any] | None = None
    # Accumulator for the current agent turn (text + tool calls).
    pending: dict[str, Any] | None = None

    def _flush_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        steps.append(pending)
        pending = None

    def _new_pending(content: str, timestamp: str | None) -> dict[str, Any]:
        nonlocal implicit_target
        step = _new_agent_step(len(steps) + 1, content, timestamp)
        implicit_target = step
        return step

    for msg in transcript.messages:
        role = msg.role

        # ---- Tool result -----------------------------------------------
        if role == "tool_result":
            call_id = msg.tool_call_id
            # Explicit call-ID → look up the registered step.
            # No call-ID → use the implicit target (cleared by prior explicit result).
            target = call_steps.get(call_id) if call_id else implicit_target
            if target is None:
                # No owning step: skip silently (unrecorded call).
                continue
            result: dict[str, Any] = {
                "content": msg.tool_result if msg.tool_result is not None else msg.content,
                "extra": {"harness_role": "tool_result"},
            }
            if call_id:
                result["source_call_id"] = call_id
            if msg.timestamp:
                result["extra"]["timestamp"] = msg.timestamp
            if msg.is_error:
                result["extra"]["is_error"] = True
            target.setdefault("observation", {"results": []})["results"].append(result)
            # An explicit call-ID result closes the implicit window; later
            # explicitly-identified results still resolve by their own ID.
            if call_id:
                implicit_target = None
            continue

        # ---- Tool-call assistant message --------------------------------
        if role == "assistant" and msg.tool_name:
            if pending is None:
                # Start a new agent turn. Native tool-call formats emit the
                # assistant text and the call in one message, so seed the step
                # with that text instead of discarding it.
                pending = _new_pending(msg.content or "", msg.timestamp)
            identifier = (
                msg.tool_call_id
                or f"norm-{pending['step_id']}-{len(pending.get('tool_calls', [])) + 1}"
            )
            if identifier not in call_steps:
                call_steps[identifier] = pending
            pending.setdefault("tool_calls", []).append(
                {
                    "tool_call_id": identifier,
                    "function_name": msg.tool_name,
                    "arguments": msg.tool_input or {},
                }
            )
            continue

        # ---- Plain text assistant message -------------------------------
        if role == "assistant":
            if pending is not None and not pending.get("tool_calls"):
                # No tool calls yet: set the text on the pending step rather
                # than emitting a new one (merges text + subsequent tool calls).
                if not pending["message"]:
                    pending["message"] = msg.content
                    if msg.timestamp and not pending.get("timestamp"):
                        pending["timestamp"] = msg.timestamp
                else:
                    # Second text message before any tool call — flush and restart.
                    _flush_pending()
                    pending = _new_pending(msg.content, msg.timestamp)
            elif pending is not None and pending.get("tool_calls"):
                # Tool calls already accumulated: flush, then start a new text step.
                _flush_pending()
                pending = _new_pending(msg.content, msg.timestamp)
            else:
                pending = _new_pending(msg.content, msg.timestamp)
            if msg.is_reasoning:
                pending.setdefault("extra", {})["is_reasoning"] = True
            continue

        # ---- User / system messages ------------------------------------
        # Flush any pending agent turn, close the implicit window.
        _flush_pending()
        implicit_target = None

        source = "user" if role == "user" else "system"
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": source,
            "message": msg.content,
        }
        if msg.timestamp:
            step["timestamp"] = msg.timestamp
        steps.append(step)

    # Flush any pending agent turn at end of transcript.
    _flush_pending()

    agent: dict[str, Any] = {"name": transcript.harness or "unknown"}
    if transcript.model:
        agent["version"] = transcript.model
    if extra_agent_fields:
        agent.update(extra_agent_fields)

    envelope: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": transcript.session_id,
        "agent": agent,
        "steps": steps,
    }
    if notes:
        envelope["notes"] = notes
    return envelope


def transcript_to_atif_json(
    transcript: SessionTranscript,
    *,
    indent: int | None = 2,
    **kwargs: Any,
) -> str:
    """Serialize a SessionTranscript to an ATIF v1.7 JSON string."""
    return json.dumps(
        session_transcript_to_atif(transcript, **kwargs),
        indent=indent,
        default=str,
    )
