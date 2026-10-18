"""Letta trajectory-v1 export adapter for gptme-sessions.

Converts a cross-harness SessionTranscript into the Letta trajectory-v1
format used by letta-ai/trajectory for agent consumption (memory formation,
dreaming, search).

Schema reference: https://github.com/letta-ai/trajectory (trajectory-v1.schema.json)

## Key differences from ATIF v1.7

- **Flat records** — no step grouping; each NormalizedMessage becomes one record.
  ATIF groups consecutive tool calls from the same assistant turn into one step.
- **No metrics** — token counts and costs are omitted by design. The format
  targets agent consumption, not benchmarking or RL pipelines.
- **args as JSON string** — tool_call.args is a JSON-encoded string, not a dict.
- **ok instead of is_error** — tool results carry ``ok: bool`` (present when the
  source exposes a reliable status); ATIF uses ``is_error`` in extra.
- **Timestamps required** — all non-meta records require an ISO 8601 timestamp.
  Records without a timestamp are dropped with a diagnostic.
- **additionalProperties: false** — no extension fields allowed on any record.
  Harness-specific metadata cannot be embedded; store it in a sidecar.
- **New roles** — ``reasoning`` (Letta) vs ``is_reasoning`` flag (ATIF);
  ``observation`` for environment feedback (not used by this adapter).

## Path to Letta ingestion

Letta's normalizeTranscript() already has an ``atif`` source adapter that
accepts ATIF-v1.0 through ATIF-v1.7. The existing
``session_transcript_to_atif()`` output can be fed directly to Letta without
this adapter. Use this module when you need the compact flat format for direct
Letta ingest or agent-consumption pipelines, or when the ATIF grouping
overhead is undesirable.

## Tool call parsing gap

gptme ``conversation.jsonl`` does not yet encode tool calls as structured
blocks (gptme/gptme-contrib#1922). Until that gap is closed,
``_normalize_gptme()`` produces text-only assistant records. This adapter
emits those as plain ``assistant`` records with no ``tool_calls``. Claude Code,
Codex, Copilot, Grok, and Pi sessions with structured tool calls produce
proper ``assistant``/``tool`` record pairs.
"""

from __future__ import annotations

import json
from typing import Any

from .transcript import SessionTranscript


_PLACEHOLDER_TS = "1970-01-01T00:00:00.000Z"


def session_transcript_to_letta(
    transcript: SessionTranscript,
    *,
    drop_missing_timestamps: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert a SessionTranscript to a Letta trajectory-v1 record list.

    Parameters
    ----------
    transcript:
        Normalized cross-harness transcript from ``read_transcript()``.
    drop_missing_timestamps:
        When ``True``, silently drop non-meta records that have no timestamp.
        When ``False`` (default), substitute ``1970-01-01T00:00:00.000Z`` and
        add a diagnostic entry.

    Returns
    -------
    tuple[list[dict], list[str]]
        ``(records, diagnostics)`` — records is a schema-valid Letta v1 array;
        diagnostics is a list of human-readable warning strings (empty when
        the conversion was clean).
    """
    diagnostics: list[str] = []

    def _resolve_ts(ts: str | None, context: str) -> str | None:
        if ts:
            # Ensure millisecond precision as Letta examples show .000Z
            return ts if "." in ts else ts.replace("Z", ".000Z").replace("+00:00", ".000Z")
        if drop_missing_timestamps:
            diagnostics.append(f"dropped record with no timestamp: {context}")
            return None
        diagnostics.append(f"substituted placeholder timestamp for: {context}")
        return _PLACEHOLDER_TS

    records: list[dict[str, Any]] = []

    # Leading meta record (no timestamp required)
    meta: dict[str, Any] = {"role": "meta", "source": transcript.harness or "unknown"}
    if transcript.project:
        meta["cwd"] = transcript.project
    if transcript.model:
        meta["model"] = transcript.model
    records.append(meta)

    for i, msg in enumerate(transcript.messages):
        role = msg.role
        context = f"message[{i}] role={role}"

        # ---- reasoning -------------------------------------------------------
        if role == "assistant" and msg.is_reasoning:
            ts = _resolve_ts(msg.timestamp, context)
            if ts is None:
                continue
            records.append({"role": "reasoning", "content": msg.content or "", "timestamp": ts})
            continue

        # ---- tool call (assistant with tool_name) ----------------------------
        if role == "assistant" and msg.tool_name:
            ts = _resolve_ts(msg.timestamp, context)
            if ts is None:
                continue
            call_id = msg.tool_call_id or f"norm-{i}"
            args_str = json.dumps(msg.tool_input or {})
            records.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": call_id, "name": msg.tool_name, "args": args_str}],
                    "timestamp": ts,
                }
            )
            continue

        # ---- plain assistant text -------------------------------------------
        if role == "assistant":
            content = (msg.content or "").strip()
            if not content:
                continue  # schema requires non-empty string when no tool_calls
            ts = _resolve_ts(msg.timestamp, context)
            if ts is None:
                continue
            records.append({"role": "assistant", "content": content, "timestamp": ts})
            continue

        # ---- tool result -----------------------------------------------------
        if role == "tool_result":
            ts = _resolve_ts(msg.timestamp, context)
            if ts is None:
                continue
            call_id = msg.tool_call_id or f"norm-result-{i}"
            rec: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": msg.tool_result if msg.tool_result is not None else msg.content,
                "timestamp": ts,
            }
            # ok is only included when the source exposes a reliable status
            # (is_error is populated from structured tool result metadata)
            if msg.is_error is not None:
                rec["ok"] = not msg.is_error
            records.append(rec)
            continue

        # ---- user / system --------------------------------------------------
        if role in ("user", "system"):
            content = (msg.content or "").strip()
            if not content:
                continue
            ts = _resolve_ts(msg.timestamp, context)
            if ts is None:
                continue
            records.append({"role": role, "content": content, "timestamp": ts})
            continue

    return records, diagnostics


def transcript_to_letta_json(
    transcript: SessionTranscript,
    *,
    indent: int | None = 2,
    **kwargs: Any,
) -> str:
    """Serialize a SessionTranscript to a Letta trajectory-v1 JSON string."""
    records, _diagnostics = session_transcript_to_letta(transcript, **kwargs)
    return json.dumps(records, indent=indent, default=str)
