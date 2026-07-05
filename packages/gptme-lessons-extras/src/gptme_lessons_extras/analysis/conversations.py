#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
# ]
# [tool.uv]
# exclude-newer = "2025-10-02T00:00:00Z"
# ///
"""
Conversation Analyzer

Analyzes conversation logs to extract learnings, patterns, and insights.
Generates structured output for lesson generation and knowledge updates.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import click

from ..utils.data import ConversationAnalysis
from ..utils.formatting import (
    count_tool_invocations,
    ensure_dir,
    is_failure_message,
    is_success_message,
    load_conversation,
    parse_timestamp,
    save_json,
    snippet,
)


def extract_tool_usage(messages: List[Dict]) -> Dict[str, int]:
    """Extract tool usage statistics from messages."""
    tool_counts: Dict[str, int] = {}

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        tools = [
            "shell",
            "ipython",
            "patch",
            "save",
            "append",
            "read",
            "browser",
            "tmux",
            "gh",
            "screenshot",
            "vision",
        ]

        for tool in tools:
            if f"```{tool}" in content or f"`{tool} " in content:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1

    return tool_counts


def extract_file_modifications(messages: List[Dict]) -> List[str]:
    """Extract list of files that were modified."""
    files = set()

    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        for pattern in ["```save ", "```patch ", "```append "]:
            if pattern in content:
                start = content.find(pattern) + len(pattern)
                end = content.find("\n", start)
                if end > start:
                    filepath = content[start:end].strip()
                    if filepath:
                        files.add(filepath)

    return sorted(files)


# Removed: Old insight generation functions (analyze_tool_effectiveness,
# analyze_decision_patterns, analyze_workflow_patterns)
# These were hand-coded detectors we moved away from in favor of
# trajectory-first episode extraction and LLM-driven learning.


def extract_episodes(messages: List[Dict]) -> List[Dict[str, Any]]:
    """Segment conversation into generic episodes: struggles, pivots, breakthroughs."""
    episodes: List[Dict[str, Any]] = []

    in_struggle = False
    struggle_start_idx = None
    struggle_start_ts = None
    failure_count = 0
    evidence_snippets: List[str] = []

    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        if role == "system" and is_failure_message(content):
            if not in_struggle:
                in_struggle = True
                struggle_start_idx = i
                struggle_start_ts = parse_timestamp(msg.get("timestamp"))
                failure_count = 0
                evidence_snippets = []
            failure_count += 1
            if len(evidence_snippets) < 2:
                evidence_snippets.append(snippet(content))

        elif role == "system" and is_success_message(content):
            success_ts = parse_timestamp(msg.get("timestamp"))

            if in_struggle and struggle_start_idx is not None:
                # Close struggle episode
                tool_invocations = count_tool_invocations(
                    messages, struggle_start_idx, i
                )
                duration_min = None
                if struggle_start_ts and success_ts:
                    duration_min = round(
                        (success_ts - struggle_start_ts).total_seconds() / 60, 2
                    )

                episodes.append(
                    {
                        "kind": "struggle",
                        "start_index": struggle_start_idx,
                        "end_index": i - 1 if i > 0 else i,
                        "start_ts": struggle_start_ts.isoformat()
                        if struggle_start_ts
                        else None,
                        "end_ts": success_ts.isoformat() if success_ts else None,
                        "error_count": failure_count,
                        "retry_depth": failure_count,
                        "tool_invocations": tool_invocations,
                        "duration_min": duration_min,
                        "evidence_snippets": evidence_snippets[:],
                        "title": f"Struggle: {failure_count} consecutive failures",
                        "context": "Trajectory segment with consecutive errors",
                        "rationale": "Consecutive failures indicate a struggle phase prior to a pivot.",
                    }
                )

                # Create pivot episode
                episodes.append(
                    {
                        "kind": "pivot",
                        "start_index": max(struggle_start_idx, 0),
                        "end_index": i,
                        "start_ts": struggle_start_ts.isoformat()
                        if struggle_start_ts
                        else None,
                        "end_ts": success_ts.isoformat() if success_ts else None,
                        "error_count": failure_count,
                        "retry_depth": failure_count,
                        "tool_invocations": tool_invocations,
                        "duration_min": duration_min,
                        "evidence_snippets": (evidence_snippets + [snippet(content)])[
                            :3
                        ],
                        "title": "Pivot: first success after failures",
                        "context": "First successful operation following a struggle",
                        "rationale": "Represents a change that resolved prior failures.",
                    }
                )

                in_struggle = False
                struggle_start_idx = None
                struggle_start_ts = None
                failure_count = 0
                evidence_snippets = []

            else:
                # Standalone success: breakthrough
                episodes.append(
                    {
                        "kind": "breakthrough",
                        "start_index": i,
                        "end_index": i,
                        "start_ts": success_ts.isoformat() if success_ts else None,
                        "end_ts": success_ts.isoformat() if success_ts else None,
                        "error_count": 0,
                        "retry_depth": 0,
                        "tool_invocations": 0,
                        "duration_min": 0,
                        "evidence_snippets": [snippet(content)],
                        "title": "Breakthrough: notable success",
                        "context": "Significant success event",
                        "rationale": "Signals a major step forward.",
                    }
                )

    # If conversation ends in struggle, record it
    if in_struggle and struggle_start_idx is not None:
        end_idx = len(messages) - 1
        end_ts = messages[end_idx].get("timestamp") if end_idx >= 0 else None
        episodes.append(
            {
                "kind": "struggle",
                "start_index": struggle_start_idx,
                "end_index": end_idx,
                "start_ts": struggle_start_ts.isoformat()
                if struggle_start_ts
                else None,
                "end_ts": end_ts,
                "error_count": failure_count,
                "retry_depth": failure_count,
                "tool_invocations": count_tool_invocations(
                    messages, struggle_start_idx, end_idx
                ),
                "duration_min": None,
                "evidence_snippets": evidence_snippets[:],
                "title": f"Struggle: {failure_count} consecutive failures (unfinished)",
                "context": "Trajectory ends during struggle",
                "rationale": "Incomplete pivot; useful for future reflection.",
            }
        )

    return episodes


def enhance_episodes_with_llm(
    episodes: List[Dict[str, Any]], messages: List[Dict]
) -> List[Dict[str, Any]]:
    """Enhance episodes with LLM-based summarization of what actually happened.

    This replaces generic episode types (struggle/pivot/breakthrough) with
    specific descriptions of the task, approach, insight, and pattern.
    """
    from ..utils.llm import llm_summarize_episode

    enhanced_episodes = []
    for episode in episodes:
        try:
            enhanced = llm_summarize_episode(episode, messages)
            enhanced_episodes.append(enhanced)
        except Exception as e:
            print(f"Warning: Failed to enhance episode: {e}")
            # Keep original episode if enhancement fails
            enhanced_episodes.append(episode)

    return enhanced_episodes


def derive_experiences(episodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Create concise, LLM-ready experiences from episodes.

    Now uses LLM-enhanced episode summaries for more specific, diverse moments.
    """
    moments: List[Dict[str, Any]] = []
    for ep in episodes:
        if ep.get("kind") in ("pivot", "breakthrough"):
            # Check if episode has LLM summary
            summary = ep.get("summary", {})

            if summary:
                # Use LLM-generated specific summary
                title = summary.get("task", "Unknown task")
                context = summary.get("approach", "")
                what_changed = summary.get("insight", "")
                rationale = summary.get("pattern", "")

                # Higher confidence if we have specific summary
                confidence = 0.8
            else:
                # Fallback to generic summary if no LLM enhancement
                failure_cnt = ep.get("error_count", 0) or 0
                title = ep.get("title") or (
                    "Pivot" if ep["kind"] == "pivot" else "Breakthrough"
                )
                context = ep.get("context", "")
                what_changed = (
                    f"After {failure_cnt} failure(s), a success occurred leading to a state change."
                    if ep["kind"] == "pivot"
                    else "A notable success occurred indicating a significant step forward."
                )
                rationale = ep.get("rationale", "")
                confidence = (
                    0.8
                    if ep["kind"] == "breakthrough"
                    else (0.7 if failure_cnt >= 2 else 0.6)
                )

            moments.append(
                {
                    "title": title,
                    "context": context,
                    "what_changed": what_changed,
                    "rationale": rationale,
                    "evidence_snippets": ep.get("evidence_snippets", [])[:3],
                    "metrics": {
                        "errors": ep.get("error_count", 0) or 0,
                        "retry_depth": ep.get("retry_depth"),
                        "duration_min": ep.get("duration_min"),
                        "tool_invocations": ep.get("tool_invocations"),
                    },
                    "episode_ref": {
                        "kind": ep.get("kind"),
                        "start_index": ep.get("start_index"),
                        "end_index": ep.get("end_index"),
                    },
                    "confidence": confidence,
                }
            )
    return moments


def generate_summary(messages: List[Dict], experiences: List[Dict]) -> str:
    """Generate a summary of the conversation."""
    first_user_msg = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                first_user_msg = content[:200]
                break

    summary_parts = []
    summary_parts.append(f"Conversation focused on: {first_user_msg}")

    if experiences:
        pivots = sum(
            1 for m in experiences if m.get("episode_ref", {}).get("kind") == "pivot"
        )
        breakthroughs = sum(
            1
            for m in experiences
            if m.get("episode_ref", {}).get("kind") == "breakthrough"
        )
        summary_parts.append(
            f"Extracted {len(experiences)} experiences ({pivots} pivots, {breakthroughs} breakthroughs)"
        )

    return " | ".join(summary_parts)


def identify_outcomes(messages: List[Dict], files_modified: List[str]) -> List[str]:
    """Identify concrete outcomes from the conversation."""
    outcomes = []

    if files_modified:
        outcomes.append(f"Modified {len(files_modified)} files")

    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if "Saved to" in content or "Patch applied" in content:
                outcomes.append(f"Successful file operation: {content[:100]}")

    return outcomes


def analyze_conversation_log(log_path: Path) -> ConversationAnalysis:
    """Analyze a complete conversation log."""
    messages = load_conversation(log_path)

    if not messages:
        raise ValueError(f"No messages found in {log_path}")

    conversation_id = log_path.name

    user_messages = sum(1 for m in messages if m.get("role") == "user")
    assistant_messages = sum(1 for m in messages if m.get("role") == "assistant")

    first_timestamp = None
    last_timestamp = None
    for msg in messages:
        ts = parse_timestamp(msg.get("timestamp"))
        if ts:
            if first_timestamp is None:
                first_timestamp = ts
            last_timestamp = ts

    duration_minutes = None
    if first_timestamp and last_timestamp:
        duration = (last_timestamp - first_timestamp).total_seconds() / 60
        duration_minutes = round(duration, 2)

    tool_counts = extract_tool_usage(messages)
    files_modified = extract_file_modifications(messages)

    # Extract trajectory episodes and experiences (trajectory-first approach)
    episodes = extract_episodes(messages)

    # Enhance episodes with LLM-based summarization
    print("Enhancing episodes with LLM summarization...")
    enhanced_episodes = enhance_episodes_with_llm(episodes, messages)

    experiences = derive_experiences(enhanced_episodes)

    summary = generate_summary(messages, experiences)
    outcomes = identify_outcomes(messages, files_modified)

    return ConversationAnalysis(
        conversation_id=conversation_id,
        timestamp=last_timestamp or datetime.now(),
        duration_minutes=duration_minutes,
        message_count=len(messages),
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_uses=tool_counts,
        files_modified=files_modified,
        summary=summary,
        outcomes=outcomes,
        metadata={
            "first_timestamp": first_timestamp.isoformat() if first_timestamp else None,
            "last_timestamp": last_timestamp.isoformat() if last_timestamp else None,
            "episodes": episodes,
            "experiences": experiences,
            "episodes_count": len(episodes),
            "experiences_count": len(experiences),
        },
    )


def save_analysis(analysis: ConversationAnalysis, output_dir: Path) -> Path:
    """Save analysis to JSON file."""
    ensure_dir(output_dir)

    timestamp_str = analysis.timestamp.strftime("%Y%m%d-%H%M%S")
    output_file = output_dir / f"{timestamp_str}-{analysis.conversation_id}.json"

    analysis_dict = {
        "conversation_id": analysis.conversation_id,
        "timestamp": analysis.timestamp.isoformat(),
        "duration_minutes": analysis.duration_minutes,
        "message_count": analysis.message_count,
        "user_messages": analysis.user_messages,
        "assistant_messages": analysis.assistant_messages,
        "tool_uses": analysis.tool_uses,
        "files_modified": analysis.files_modified,
        "summary": analysis.summary,
        "outcomes": analysis.outcomes,
        "metadata": analysis.metadata,
    }

    save_json(analysis_dict, output_file)
    return output_file


@click.command()
@click.argument("log_path", type=click.Path(exists=False))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default="knowledge/meta/conversations",
    help="Output directory for analysis",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def main(log_path: str, output_dir: str, verbose: bool):
    """Analyze a conversation log and extract insights.

    LOG_PATH can be either:
      - A conversation directory (e.g., logs/2025-04-19-hopping-sad-alien/)
      - A conversation.jsonl file
      - The string 'latest' to analyze the most recent conversation
    """
    log_path_obj = Path(log_path)
    output_dir_obj = Path(output_dir)

    if log_path == "latest":
        logs_dir = Path("logs")
        if not logs_dir.exists():
            click.echo("Error: logs/ directory not found", err=True)
            sys.exit(1)

        conversation_dirs = [d for d in logs_dir.iterdir() if d.is_dir()]
        if not conversation_dirs:
            click.echo("Error: No conversation directories found", err=True)
            sys.exit(1)

        log_path_obj = max(conversation_dirs, key=lambda d: d.stat().st_mtime)
        click.echo(f"Analyzing latest conversation: {log_path_obj.name}")

    if log_path_obj.is_file():
        log_path_obj = log_path_obj.parent

    try:
        analysis = analyze_conversation_log(log_path_obj)
        output_file = save_analysis(analysis, output_dir_obj)

        click.echo(f"✓ Analysis complete: {output_file}")

        if verbose:
            click.echo("\nConversation Statistics:")
            click.echo(
                f"  Duration: {analysis.duration_minutes} minutes"
                if analysis.duration_minutes
                else "  Duration: unknown"
            )
            click.echo(
                f"  Messages: {analysis.message_count} ({analysis.user_messages} user, {analysis.assistant_messages} assistant)"
            )
            click.echo(f"  Tools used: {len(analysis.tool_uses)}")
            for tool, count in sorted(analysis.tool_uses.items(), key=lambda x: -x[1]):
                click.echo(f"    - {tool}: {count}")
            click.echo(f"  Files modified: {len(analysis.files_modified)}")
            click.echo(f"  Episodes: {analysis.metadata.get('episodes_count', 0)}")
            click.echo(
                f"  Experiences: {analysis.metadata.get('experiences_count', 0)}"
            )

    except Exception as e:
        click.echo(f"Error analyzing conversation: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
