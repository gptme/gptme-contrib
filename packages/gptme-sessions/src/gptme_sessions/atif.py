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
- Call ID linking: tool_call_id is authoritative; duplicate call IDs raise
  ValueError rather than guessing which invocation owns a result. Unlinked
  results attach to the latest tool-owning step, or the latest text step if
  no call has opened a window. Explicit results and user/system boundaries
  close the window; a new tool call reopens it.
- Metrics: populated from ``NormalizedMessage.usage`` when available. gptme
  sessions carry per-message usage (input_tokens/output_tokens) in record
  ``metadata.usage``; ``_normalize_gptme()`` extracts it. Claude Code, Codex,
  and other harnesses may not populate it. extra_agent_fields only enriches
  the agent block.
- Images, attachment payloads, subagent trees, and reasoning deltas are omitted.
- An empty-message agent step is valid per ATIF when it carries tool_calls.
- gptme ``conversation.jsonl`` normalization does not yet parse tool calls out
  of message content, so a gptme transcript yields text-only steps with no
  ``tool_calls``/``observation``. The envelope validates, but tool structure is
  lost; use the raw-message converter in ``benchmark/pier-gptme/`` when tool
  fidelity matters.
"""

from __future__ import annotations

import json
from typing import Any

from .transcript import SessionTranscript


def _accumulate_metrics(step: dict[str, Any], usage: dict[str, Any]) -> None:
    """Merge per-message token counts into the step-level metrics block.

    Field names follow ATIF v1.7 ``Metrics`` (``prompt_tokens``,
    ``completion_tokens``, ``cached_tokens``, ``cost_usd``); the model is
    ``extra="forbid"``, so harness-native names like ``input_tokens`` would
    make the whole trajectory fail ``Trajectory.model_validate``.

    gptme reports ``input_tokens`` *excluding* cache reads, while ATIF defines
    ``prompt_tokens`` as the total including cached and non-cached. Cache
    counters are therefore folded into ``prompt_tokens`` and also reported
    separately as ``cached_tokens``. Absent keys stay absent — missing usage is
    unknown, not zero.
    """
    cur = step.get("metrics", {})

    def _num(key: str) -> int:
        return int(cur.get(key) or 0)

    prompt = _num("prompt_tokens") + int(usage.get("input_tokens") or 0)
    prompt += int(usage.get("cache_read_tokens") or 0)
    prompt += int(usage.get("cache_creation_tokens") or 0)
    completion = _num("completion_tokens") + int(usage.get("output_tokens") or 0)
    cached = _num("cached_tokens") + int(usage.get("cache_read_tokens") or 0)

    metrics: dict[str, Any] = {}
    if prompt:
        metrics["prompt_tokens"] = prompt
    if completion:
        metrics["completion_tokens"] = completion
    if cached:
        metrics["cached_tokens"] = cached
    cost = usage.get("cost")
    if cost is None:
        cost = usage.get("cost_usd")
    if cost is not None:
        metrics["cost_usd"] = float(cur.get("cost_usd") or 0.0) + float(cost)
    if metrics:
        step["metrics"] = metrics


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

    Raises
    ------
    ValueError
        Two tool invocations share a source call ID, making result ownership
        ambiguous. Source IDs must be unique within the transcript.
    """
    steps: list[dict[str, Any]] = []
    # Reserve source IDs so generated IDs cannot collide with a later call.
    source_call_ids = {msg.tool_call_id for msg in transcript.messages if msg.tool_call_id}
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

    def _new_pending(
        content: str, timestamp: str | None, *, update_implicit: bool = True
    ) -> dict[str, Any]:
        nonlocal implicit_target
        step = _new_agent_step(len(steps) + 1, content, timestamp)
        if update_implicit:
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
            elif msg.content:
                # Pending step exists (from an earlier text part of this turn);
                # keep the text carried by the tool-call message too.
                pending["message"] = (
                    f"{pending['message']}\n\n{msg.content}" if pending["message"] else msg.content
                )
            identifier = msg.tool_call_id
            if not identifier:
                suffix = len(pending.get("tool_calls", [])) + 1
                identifier = f"norm-{pending['step_id']}-{suffix}"
                while identifier in source_call_ids or identifier in call_steps:
                    suffix += 1
                    identifier = f"norm-{pending['step_id']}-{suffix}"
            if identifier in call_steps:
                raise ValueError(f"Duplicate tool_call_id: {identifier}")
            call_steps[identifier] = pending
            pending.setdefault("tool_calls", []).append(
                {
                    "tool_call_id": identifier,
                    "function_name": msg.tool_name,
                    "arguments": msg.tool_input or {},
                }
            )
            if msg.usage:
                _accumulate_metrics(pending, msg.usage)
            # A new call opens its own implicit result window, even when text
            # or an explicitly linked result previously changed the target.
            implicit_target = pending
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
                    # Preserve an earlier tool-owning target across text-only steps.
                    _flush_pending()
                    pending = _new_pending(
                        msg.content,
                        msg.timestamp,
                        update_implicit=implicit_target is None
                        or not implicit_target.get("tool_calls"),
                    )
            elif pending is not None and pending.get("tool_calls"):
                # Tool calls already accumulated: flush, then start a new text step.
                # Do NOT update implicit_target: unlinked results for calls in the
                # flushed step must still attach to it, not to this text-only step.
                _flush_pending()
                pending = _new_pending(msg.content, msg.timestamp, update_implicit=False)
            else:
                pending = _new_pending(msg.content, msg.timestamp)
            if msg.is_reasoning:
                pending.setdefault("extra", {})["is_reasoning"] = True
            if msg.usage:
                _accumulate_metrics(pending, msg.usage)
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

    # ATIF v1.7 `Agent` requires both `name` and `version`, and keeps the LLM
    # identity in `model_name`. Putting the model string in `version` (the old
    # behaviour) both dropped the model field and made consumers comparing
    # agent versions see a model id. `version` is the harness version, which a
    # transcript does not always carry; callers pass it via extra_agent_fields
    # (e.g. {"version": "0.35.1"}), otherwise "unknown" keeps the envelope valid.
    agent: dict[str, Any] = {"name": transcript.harness or "unknown", "version": "unknown"}
    if transcript.model:
        agent["model_name"] = transcript.model
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
