#!/usr/bin/env python3
"""
Detect potential lesson patterns from conversation logs.

This script analyzes gptme conversation logs to identify:
- Error patterns and failures
- Repeated issues across conversations
- Context around problems
- Potential lesson candidates

Usage:
    ./scripts/detect-lesson-patterns.py [--days N] [--min-occurrences N] [--output FILE]
"""

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

# Common error patterns to detect
ERROR_PATTERNS = {
    "bash_error": r"bash: .+: (command not found|No such file|syntax error)",
    "python_error": r"(Traceback|Error:|Exception:).+",
    "git_error": r"(fatal|error): .+",
    "timeout": r"(timeout|timed out)",
    "permission_denied": r"Permission denied",
    "not_found": r"(not found|does not exist|cannot find)",
    "invalid_syntax": r"(invalid syntax|SyntaxError)",
    "type_error": r"TypeError:",
    "attribute_error": r"AttributeError:",
    "key_error": r"KeyError:",
}


@dataclass
class ErrorOccurrence:
    """Represents an error found in a conversation."""

    pattern_name: str
    error_text: str
    context_before: str
    context_after: str
    conversation_id: str
    timestamp: str
    message_index: int


@dataclass
class LessonCandidate:
    """A potential lesson identified from error patterns."""

    pattern_name: str
    occurrences: List[ErrorOccurrence]
    count: int
    conversations: List[str]
    suggested_title: str
    category: str


def parse_conversation_log(log_path: Path) -> List[Dict]:
    """Parse a JSONL conversation log file."""
    messages = []
    try:
        with open(log_path) as f:
            for line in f:
                if line.strip():
                    msg = json.loads(line)
                    messages.append(msg)
    except Exception as e:
        print(f"Warning: Error parsing {log_path}: {e}")
    return messages


def find_error_in_content(content: str) -> List[Tuple[str, str]]:
    """Find error patterns in message content."""
    errors = []
    for pattern_name, pattern in ERROR_PATTERNS.items():
        matches = re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            error_text = match.group(0)
            errors.append((pattern_name, error_text))
    return errors


def extract_context(content: str, match_pos: int, window: int = 200) -> Tuple[str, str]:
    """Extract context around an error match."""
    start = max(0, match_pos - window)
    end = min(len(content), match_pos + window)

    context_before = content[start:match_pos].strip()
    context_after = content[match_pos:end].strip()

    return context_before, context_after


def analyze_conversation(log_path: Path, conversation_id: str) -> List[ErrorOccurrence]:
    """Analyze a single conversation for error patterns."""
    messages = parse_conversation_log(log_path)
    occurrences = []

    for i, msg in enumerate(messages):
        # Skip hidden system messages
        if msg.get("hide"):
            continue

        content = msg.get("content", "")
        role = msg.get("role", "")

        # Look for errors in system responses (tool execution)
        if role == "system":
            errors = find_error_in_content(content)
            for pattern_name, error_text in errors:
                # Find error position for context extraction
                error_pos = content.find(error_text)
                context_before, context_after = extract_context(content, error_pos)

                occurrence = ErrorOccurrence(
                    pattern_name=pattern_name,
                    error_text=error_text,
                    context_before=context_before,
                    context_after=context_after,
                    conversation_id=conversation_id,
                    timestamp=msg.get("timestamp", ""),
                    message_index=i,
                )
                occurrences.append(occurrence)

    return occurrences


def get_recent_conversations(logs_dir: Path, days: int = 30) -> List[Tuple[str, Path]]:
    """Get conversation logs from the last N days."""
    cutoff = datetime.now() - timedelta(days=days)
    conversations = []

    for log_dir in logs_dir.iterdir():
        if not log_dir.is_dir():
            continue

        conversation_id = log_dir.name
        log_file = log_dir / "conversation.jsonl"

        if not log_file.exists():
            continue

        # Check if conversation is recent
        try:
            # Extract date from conversation ID if available
            if conversation_id.startswith("2025-"):
                date_parts = conversation_id.split("-")[0:3]
                date_str = "-".join(date_parts)
                conv_date = datetime.strptime(date_str, "%Y-%m-%d")
                if conv_date >= cutoff:
                    conversations.append((conversation_id, log_file))
            else:
                # Use file modification time as fallback
                mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                if mtime >= cutoff:
                    conversations.append((conversation_id, log_file))
        except Exception:
            # If date parsing fails, include the conversation
            conversations.append((conversation_id, log_file))

    return sorted(conversations, reverse=True)  # Most recent first


def group_by_pattern(
    occurrences: List[ErrorOccurrence],
) -> Dict[str, List[ErrorOccurrence]]:
    """Group error occurrences by pattern type."""
    by_pattern = defaultdict(list)
    for occ in occurrences:
        by_pattern[occ.pattern_name].append(occ)
    return dict(by_pattern)


def suggest_lesson_category(pattern_name: str) -> str:
    """Suggest a lesson category based on error pattern."""
    category_map = {
        "bash_error": "tools",
        "python_error": "tools",
        "git_error": "workflow",
        "timeout": "tools",
        "permission_denied": "workflow",
        "not_found": "workflow",
        "invalid_syntax": "tools",
        "type_error": "tools",
        "attribute_error": "tools",
        "key_error": "tools",
    }
    return category_map.get(pattern_name, "patterns")


def suggest_lesson_title(pattern_name: str, error_text: str) -> str:
    """Suggest a lesson title based on error pattern."""
    # Extract key terms from error
    if "command not found" in error_text:
        cmd = error_text.split(":")[1].split(":")[0].strip()
        return f"Missing Command: {cmd}"
    elif "No such file" in error_text:
        return "File Path Issues"
    elif "syntax error" in error_text.lower():
        return "Shell Syntax Errors"
    elif "Traceback" in error_text:
        return "Python Execution Errors"
    elif "timeout" in error_text.lower():
        return "Command Timeouts"
    elif "Permission denied" in error_text:
        return "Permission Issues"
    else:
        return f"{pattern_name.replace('_', ' ').title()} Pattern"


def create_lesson_candidates(
    by_pattern: Dict[str, List[ErrorOccurrence]], min_occurrences: int = 2
) -> List[LessonCandidate]:
    """Create lesson candidates from grouped errors."""
    candidates = []

    for pattern_name, occurrences in by_pattern.items():
        if len(occurrences) < min_occurrences:
            continue

        # Group by unique conversations
        conversations = list(set(occ.conversation_id for occ in occurrences))

        # Get a representative error text
        representative = occurrences[0]

        candidate = LessonCandidate(
            pattern_name=pattern_name,
            occurrences=occurrences,
            count=len(occurrences),
            conversations=conversations,
            suggested_title=suggest_lesson_title(
                pattern_name, representative.error_text
            ),
            category=suggest_lesson_category(pattern_name),
        )
        candidates.append(candidate)

    # Sort by frequency
    candidates.sort(key=lambda c: c.count, reverse=True)
    return candidates


def format_lesson_candidate(candidate: LessonCandidate) -> str:
    """Format a lesson candidate for display."""
    output = []
    output.append(f"## {candidate.suggested_title}")
    output.append(f"**Pattern**: {candidate.pattern_name}")
    output.append(f"**Category**: lessons/{candidate.category}/")
    output.append(
        f"**Occurrences**: {candidate.count} across {len(candidate.conversations)} conversations"
    )
    output.append("")

    # Show a few examples
    output.append("### Example Errors:")
    for i, occ in enumerate(candidate.occurrences[:3]):
        output.append(f"\n**Example {i + 1}** (Conversation: {occ.conversation_id}):")
        output.append("```")
        output.append(occ.error_text)
        output.append("```")

        if occ.context_before:
            output.append(f"Context: ...{occ.context_before[-100:]}...")

    output.append("")
    output.append("### Next Steps:")
    output.append("1. Review the error examples")
    output.append("2. Identify the root cause")
    output.append("3. Create a lesson following the template")
    output.append("4. Add anti-pattern and recommended pattern")
    output.append("5. Include verification checklist")
    output.append("")
    output.append("---")
    output.append("")

    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(
        description="Detect potential lesson patterns from conversation logs"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Analyze conversations from last N days (default: 7)",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=2,
        help="Minimum occurrences to suggest a lesson (default: 2)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file for lesson candidates (default: stdout)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed progress",
    )

    args = parser.parse_args()

    # Find conversation logs
    logs_dir = Path.home() / ".local/share/gptme/logs"
    if not logs_dir.exists():
        print(f"Error: Logs directory not found: {logs_dir}")
        return 1

    print(f"🔍 Analyzing conversations from last {args.days} days...")
    conversations = get_recent_conversations(logs_dir, days=args.days)
    print(f"Found {len(conversations)} conversations to analyze")

    # Analyze all conversations
    all_occurrences = []
    for conv_id, log_path in conversations:
        if args.verbose:
            print(f"  Analyzing: {conv_id}")
        occurrences = analyze_conversation(log_path, conv_id)
        all_occurrences.extend(occurrences)

    print(f"Found {len(all_occurrences)} total error occurrences")

    # Group and create candidates
    by_pattern = group_by_pattern(all_occurrences)
    candidates = create_lesson_candidates(
        by_pattern, min_occurrences=args.min_occurrences
    )

    print(f"Identified {len(candidates)} potential lesson candidates")
    print("")

    # Format output
    output_lines = []
    output_lines.append("# Lesson Candidates from Pattern Analysis")
    output_lines.append("")
    output_lines.append(f"Generated: {datetime.now().isoformat()}")
    output_lines.append(
        f"Analyzed: {len(conversations)} conversations from last {args.days} days"
    )
    output_lines.append(f"Found: {len(all_occurrences)} error occurrences")
    output_lines.append(f"Candidates: {len(candidates)} potential lessons")
    output_lines.append("")
    output_lines.append("---")
    output_lines.append("")

    for candidate in candidates:
        output_lines.append(format_lesson_candidate(candidate))

    output_text = "\n".join(output_lines)

    # Write output
    if args.output:
        args.output.write_text(output_text)
        print(f"✅ Results written to: {args.output}")
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    exit(main())
