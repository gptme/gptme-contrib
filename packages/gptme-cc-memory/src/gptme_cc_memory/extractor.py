"""Heuristic session memory extractor — no LLM dependency.

Reads Claude Code session trajectories (JSONL) and extracts:

1. **Corrections** — user messages that imply behavioral rules
2. **Confirmations** — user agreement or acceptance
3. **Pending items** — unfinished work or follow-ups
4. **Guidance blocks** — structured formatting hints from the model
5. **Session context** — structural session data (goal, tool usage, last turn)

The extractor is heuristic only. It uses regex patterns and simple scoring,
not an LLM, so it can run asynchronously in the stop-hook with zero API cost.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Patterns that signal a user correction (behavioral rule)
CORRECTION_PATTERNS: list[tuple[str, str]] = [
    # Direct instruction patterns
    (r"don't\s+(use|do|run|try|make|write|create|add|set|start|let)\b", "restrictive_directive"),
    (
        r"never\s+(use|do|run|try|make|write|create|add|set|start|let|bypass|skip|ignore)\b",
        "strong_directive",
    ),
    (
        r"always\s+(use|do|run|write|add|check|verify|validate|include|ensure)\b",
        "positive_directive",
    ),
    (r"stop\s+(using|doing|trying|making)\b", "cessation"),
    (r"you\s+should\s+(not|never|always)\b", "prescriptive_advice"),
    (r"please\s+(don't|do\s+not|never|always)\b", "polite_directive"),
    # Negative feedback patterns
    (
        r"that('s|\sis|\swas)?\s+(wrong|incorrect|bad|not\s+right|not\s+what\s+I\s+meant)\b",
        "negative_feedback",
    ),
    (r"(that|this)\s+(isn't|wasn't|doesn't|didn't)\s+(what|the)\b", "negative_feedback"),
    (r"(i\s+)?(didn't|did\s+not)\s+(ask|mean|want)\b", "unwanted_action"),
    # Correction prefixes
    (r"^actually[,\s]", "correction_prefix"),
    (r"^no[,\s]+", "direct_correction"),
    (r"^wait[,\s]+", "midcourse_correction"),
    # Specific behavioral patterns
    (
        r"(\w+)\s+is\s+(not\s+)?(needed|unnecessary|not\s+what\s+I\s+asked\s+for)\b",
        "scope_correction",
    ),
    (r"next\s+time[,\s]+(don't|please|just|use)\b", "future_instruction"),
    (r"i\s+prefer\b", "preference_statement"),
]

# Patterns that signal user confirmation (valuable to record)
CONFIRMATION_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(looks?\s+)?(great|good|perfect|excellent|correct|right|yes|thanks|thank\s+you)\b",
        "positive_confirmation",
    ),
    (r"\b(that('s|\sis|\sworks)|\bworks\s+well)\s*[.!]?\s*$", "works_confirmation"),
    (r"\b(approved?|accepted?|confirmed?)\b", "explicit_approval"),
]

# A positive word directly negated ("not right", "isn't good", "not quite
# correct") is negative feedback and must not be recorded as a confirmation.
_NEGATED_CONFIRMATION_RE = re.compile(
    r"\b(not|never|no|isn't|wasn't|aren't|doesn't|don't|didn't)\s+(\w+\s+)?"
    r"(great|good|perfect|excellent|correct|right|working|works?|approved?|accepted?|confirmed?)\b",
    re.IGNORECASE,
)

# Patterns for structured guidance in model responses
GUIDANCE_PATTERNS: list[str] = [
    r"(reminder|rule|guideline|principle|convention|policy|standard|practice)s?:",
    r"(always|never|always\s+use|don't\s+use)\s+(.{10,100})",
]


@dataclass
class ExtractionResult:
    """Results from extracting memory signals from a session trajectory."""

    corrections: list[dict[str, Any]] = field(default_factory=list)
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    guidance_blocks: list[str] = field(default_factory=list)
    session_context: dict[str, Any] = field(default_factory=dict)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in same dir + ``os.replace``).

    A failed write (e.g. ENOSPC) leaves the previous file content intact
    instead of a truncated file.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # Exclusive creation with private permissions, independent of the umask.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
        tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _content_text(content: Any) -> str:
    """Flatten message content to plain text.

    Strings pass through; a list of content blocks contributes only its
    ``{"type": "text"}`` blocks (tool_use / tool_result / thinking are
    dropped). Anything else (missing, null, dict, ...) yields "".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _normalize_message(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one transcript record to the internal ``{role, content}`` shape.

    Supports both the flat ``{"role": "human", "content": ...}`` format and the
    real Claude Code transcript format
    ``{"type": "user", "message": {"role": "user", "content": str | [blocks]}}``.
    Returns ``None`` for records with no human/assistant text (summaries,
    meta lines, tool_result-only user turns).
    """
    inner = record.get("message")
    if isinstance(inner, dict) and "role" not in record:
        if record.get("isMeta"):
            return None
        role = inner.get("role") or record.get("type")
        raw_content = inner.get("content")
    else:
        role = record.get("role")
        raw_content = record.get("content")

    if role == "user":
        role = "human"
    if role not in ("human", "assistant"):
        return None

    content = _content_text(raw_content)
    tool_uses = 0
    if isinstance(raw_content, list):
        tool_uses = sum(
            1 for b in raw_content if isinstance(b, dict) and b.get("type") == "tool_use"
        )
    if not content.strip() and not tool_uses:
        return None

    msg: dict[str, Any] = {"role": role, "content": content}
    if tool_uses:
        msg["tool_use_count"] = tool_uses
    return msg


def read_trajectory(trajectory_path: Path) -> list[dict[str, Any]]:
    """Read a Claude Code trajectory file (JSONL, one JSON object per line).

    Each line is normalized to ``{"role": "human"|"assistant", "content": str}``.
    Undecodable bytes are replaced and unparseable lines are skipped, so one
    bad line never aborts the whole extraction.
    """
    messages: list[dict[str, Any]] = []
    try:
        text = trajectory_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        msg = _normalize_message(record)
        if msg is not None:
            messages.append(msg)
    return messages


def _message_text(msg: dict[str, Any]) -> str:
    """Return a message's text with typographic apostrophes normalized."""
    content = msg.get("content")
    text = content if isinstance(content, str) else _content_text(content)
    return text.replace("\u2019", "'").replace("\u2018", "'")


def detect_corrections(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect user corrections in a trajectory.

    Scans user messages for correction patterns and extracts the relevant text.
    """
    corrections: list[dict[str, Any]] = []

    for msg in messages:
        if msg.get("role") != "human":
            continue

        content = _message_text(msg)
        if not content:
            continue

        # Check each correction pattern
        for pattern, label in CORRECTION_PATTERNS:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                # Extract context around the match
                start = max(0, match.start() - 80)
                end = min(len(content), match.end() + 120)
                context = content[start:end].strip()

                corrections.append(
                    {
                        "type": label,
                        "pattern": pattern,
                        "matched": match.group(0),
                        "context": context,
                        "full_text": content[:500],
                    }
                )
                break  # One correction type per message to avoid noise

    return corrections


def detect_confirmations(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect user confirmations in a trajectory."""
    confirmations: list[dict[str, Any]] = []

    for msg in messages:
        if msg.get("role") != "human":
            continue

        content = _message_text(msg).strip()
        if not content or len(content) > 200:
            continue  # Long messages are probably substantive, not confirmations
        if _NEGATED_CONFIRMATION_RE.search(content):
            continue  # "that's not right" is negative feedback, not a confirmation

        for pattern, label in CONFIRMATION_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                confirmations.append(
                    {
                        "type": label,
                        "pattern": pattern,
                        "full_text": content[:200],
                    }
                )
                break

    return confirmations


def detect_guidance_blocks(
    messages: list[dict[str, Any]],
) -> list[str]:
    """Detect structured guidance or rules in model responses."""
    blocks: list[str] = []

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        content = _message_text(msg)
        if not content:
            continue

        for pattern in GUIDANCE_PATTERNS:
            matches = re.finditer(pattern, content, re.IGNORECASE)
            for match in matches:
                start = max(0, match.start() - 30)
                end = min(len(content), match.end() + 100)
                snippet = content[start:end].strip()
                if snippet and snippet not in blocks:
                    blocks.append(snippet)

    return blocks


def extract_session_context(
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract structural session data from a trajectory.

    Captures: original goal (first user message), last assistant turn,
    tool call count, and session boundaries.
    """
    human_texts = [t for m in messages if m.get("role") == "human" and (t := _message_text(m))]
    assistant_texts = [
        t for m in messages if m.get("role") == "assistant" and (t := _message_text(m))
    ]

    first_user_msg = human_texts[0][:200] if human_texts else None
    last_assistant_msg = assistant_texts[-1][:300] if assistant_texts else None

    # Count tool calls across all messages: flat-format "Tool Use:" markers
    # plus structured tool_use blocks counted during normalization.
    tool_call_count = 0
    for msg in messages:
        tool_call_count += _message_text(msg).count("Tool Use:")
        tool_uses = msg.get("tool_use_count", 0)
        tool_call_count += tool_uses if isinstance(tool_uses, int) else 0

    return {
        "goal": first_user_msg,
        "last_turn": last_assistant_msg,
        "tool_call_count": tool_call_count,
        "message_count": len(messages),
    }


def extract_memories(
    trajectory_path: Path,
) -> ExtractionResult:
    """Run all extractors on a trajectory file and return combined results.

    This is the main entry point for the stop-hook.
    """
    messages = read_trajectory(trajectory_path)
    if not messages:
        return ExtractionResult()

    return ExtractionResult(
        corrections=detect_corrections(messages),
        confirmations=detect_confirmations(messages),
        guidance_blocks=detect_guidance_blocks(messages),
        session_context=extract_session_context(messages),
    )


def _one_line(text: Any) -> str:
    """Collapse whitespace so user text cannot break the markdown block structure."""
    return re.sub(r"\s+", " ", str(text)).strip()


def format_pending_updates(result: ExtractionResult) -> str:
    """Format extraction results as a pending-updates markdown block.

    Every non-empty block starts with a dated ``## Pending — YYYY-MM-DD HH:MM``
    header so the injector can prune the whole block (including its
    Confirmations/Guidance subsections) once it is stale.
    """
    if not (result.corrections or result.confirmations or result.guidance_blocks):
        return ""

    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if result.corrections:
        lines.append(f"## Pending — {now} (corrections)")
        for c in result.corrections:
            lines.append(f'- **{c["type"]}**: "{_one_line(c["matched"])}"')
            lines.append(f"  > {_one_line(c['context'])[:200]}")
        lines.append("")
    else:
        lines.append(f"## Pending — {now}")

    if result.confirmations:
        lines.append("### Confirmations")
        for c in result.confirmations:
            lines.append(f'- **{c["type"]}**: "{_one_line(c["full_text"])[:120]}"')
        lines.append("")

    if result.guidance_blocks:
        lines.append("### Guidance")
        for g in result.guidance_blocks:
            lines.append(f"- {_one_line(g)[:200]}")
        lines.append("")

    return "\n".join(lines).strip()


def format_session_context(result: ExtractionResult) -> str:
    """Format session context as a one-shot markdown block."""
    ctx = result.session_context
    if not ctx.get("goal"):
        return ""

    lines = ["## Previous Session"]
    lines.append(f"**Goal**: {ctx['goal'][:200]}")
    if ctx.get("last_turn"):
        lines.append(f"**Last turn**: {ctx['last_turn'][:200]}")
    lines.append(
        f"**Messages**: {ctx.get('message_count', '?')} | **Tool calls**: {ctx.get('tool_call_count', '?')}"
    )
    return "\n".join(lines).strip()


def main() -> None:
    """CLI entry point for the extractor.

    Reads a trajectory file path from the first CLI argument or from the
    ``CC_TRAJECTORY_FILE`` environment variable.
    """
    trajectory_path: Path | None = None

    # Try CLI arg first
    if len(sys.argv) > 1:
        trajectory_path = Path(sys.argv[1])
    else:
        env_path = os.environ.get("CC_TRAJECTORY_FILE")
        if env_path:
            trajectory_path = Path(env_path)

    if not trajectory_path or not trajectory_path.exists():
        print("Usage: python -m gptme_cc_memory.extractor <trajectory.jsonl>", file=sys.stderr)
        sys.exit(1)

    result = extract_memories(trajectory_path)

    # Match hooks.prompt_submit: GPTME_CC_MEMORY_DIR points at the workspace root.
    memory_dir_env = os.environ.get("GPTME_CC_MEMORY_DIR")
    if memory_dir_env:
        memory_dir = Path(memory_dir_env) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        updates = format_pending_updates(result)
        if updates:
            updates_file = memory_dir / "pending-updates.md"
            # Append to existing content
            existing = ""
            if updates_file.exists():
                existing = updates_file.read_text(encoding="utf-8").strip()
            combined = f"{existing}\n\n{updates}" if existing else updates
            atomic_write_text(updates_file, combined.strip() + "\n")

        ctx = format_session_context(result)
        if ctx:
            atomic_write_text(memory_dir / "pending-session-context.md", ctx + "\n")

    # Also print summary to stdout
    print(
        f"Extracted {len(result.corrections)} corrections, {len(result.confirmations)} confirmations, "
        f"{len(result.guidance_blocks)} guidance blocks"
    )


if __name__ == "__main__":
    main()
