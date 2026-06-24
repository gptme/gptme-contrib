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

# Patterns for pending items and follow-ups
PENDING_ITEM_PATTERNS: list[str] = [
    r"(next|remaining|outstanding|unfinished|todo)\s+(step|task|item|work)",
    r"(to\s+do|follow.?up|check\s+back|look\s+into|investigate)",
    r"(i'll|we'll|let's)\s+(come\s+back|continue|finish|address)",
    r"(pending|deferred|postponed|on\s+hold)",
]

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
    pending_items: list[str] = field(default_factory=list)
    guidance_blocks: list[str] = field(default_factory=list)
    session_context: dict[str, Any] = field(default_factory=dict)


def read_trajectory(trajectory_path: Path) -> list[dict[str, Any]]:
    """Read a Claude Code trajectory file (JSONL, one JSON object per line)."""
    messages: list[dict[str, Any]] = []
    try:
        for line in trajectory_path.read_text(encoding="utf-8").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if isinstance(msg, dict):
                    messages.append(msg)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return messages


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

        content = str(msg.get("content", ""))
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

        content = str(msg.get("content", "")).strip()
        if not content or len(content) > 200:
            continue  # Long messages are probably substantive, not confirmations

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


def detect_pending_items(
    messages: list[dict[str, Any]],
) -> list[str]:
    """Detect pending items and follow-ups in a trajectory."""
    items: list[str] = []
    seen: set[str] = set()

    for msg in messages:
        # Check both user and assistant messages
        content = str(msg.get("content", ""))
        if not content or len(content) > 1000:
            continue

        for pattern in PENDING_ITEM_PATTERNS:
            matches = re.finditer(pattern, content, re.IGNORECASE)
            for match in matches:
                start = max(0, match.start() - 40)
                end = min(len(content), match.end() + 80)
                snippet = content[start:end].strip()
                if snippet and snippet not in seen:
                    seen.add(snippet)
                    items.append(snippet)

    return items


def detect_guidance_blocks(
    messages: list[dict[str, Any]],
) -> list[str]:
    """Detect structured guidance or rules in model responses."""
    blocks: list[str] = []

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        content = str(msg.get("content", ""))
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
    human_messages = [m for m in messages if m.get("role") == "human"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    first_user_msg = human_messages[0]["content"][:200] if human_messages else None
    last_assistant_msg = assistant_messages[-1]["content"][:300] if assistant_messages else None

    # Count tool calls across all messages
    tool_call_count = 0
    for msg in messages:
        content = str(msg.get("content", ""))
        tool_call_count += content.count("Tool Use:")

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
        pending_items=detect_pending_items(messages),
        guidance_blocks=detect_guidance_blocks(messages),
        session_context=extract_session_context(messages),
    )


def format_pending_updates(result: ExtractionResult) -> str:
    """Format extraction results as a pending-updates markdown block."""
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if result.corrections:
        lines.append(f"## Pending — {now} (corrections)")
        for c in result.corrections:
            lines.append(f"- **{c['type']}**: \"{c['matched']}\"")
            lines.append(f"  > {c['context'][:200]}")
        lines.append("")

    if result.confirmations:
        lines.append("### Confirmations")
        for c in result.confirmations:
            lines.append(f"- **{c['type']}**: \"{c['full_text'][:120]}\"")
        lines.append("")

    if result.guidance_blocks:
        lines.append("### Guidance")
        for g in result.guidance_blocks:
            lines.append(f"- {g[:200]}")
        lines.append("")

    return "\n".join(lines).strip()


def format_pending_items(result: ExtractionResult) -> str:
    """Format extraction results as a pending-items markdown block."""
    if not result.pending_items:
        return ""

    lines = ["## Pending Items"]
    for item in result.pending_items[:5]:  # Limit to 5 items
        lines.append(f"- {item[:200]}")
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
            updates_file.write_text(combined.strip() + "\n", encoding="utf-8")

        items = format_pending_items(result)
        if items:
            (memory_dir / "pending-items.md").write_text(items + "\n", encoding="utf-8")

        ctx = format_session_context(result)
        if ctx:
            (memory_dir / "pending-session-context.md").write_text(ctx + "\n", encoding="utf-8")

    # Also print summary to stdout
    print(
        f"Extracted {len(result.corrections)} corrections, {len(result.confirmations)} confirmations, "
        f"{len(result.pending_items)} pending items, {len(result.guidance_blocks)} guidance blocks"
    )


if __name__ == "__main__":
    main()
