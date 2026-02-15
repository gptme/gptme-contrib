"""
Claude Code backend for journal summarization.

Uses the `claude -p` CLI to generate summaries instead of regex-based extraction.
This provides better quality summaries and saves tokens in the main gptme session.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any


def call_claude_code(prompt: str, timeout: int = 120) -> str:
    """
    Call Claude Code CLI with a prompt.

    Args:
        prompt: The prompt to send to Claude Code
        timeout: Maximum time to wait for response (seconds)

    Returns:
        The response text from Claude Code

    Raises:
        subprocess.TimeoutExpired: If the command times out
        subprocess.CalledProcessError: If the command fails
    """
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["claude", "-p"], result.stdout, result.stderr
        )
    return result.stdout.strip()


def extract_json_from_response(response: str) -> dict[str, Any]:
    """
    Extract JSON from Claude Code response.

    Claude Code may include markdown code blocks or explanatory text.
    This function extracts the JSON portion.
    """
    # Try to find JSON in code blocks first
    json_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", response)
    if json_match:
        try:
            result: dict[str, Any] = json.loads(json_match.group(1))
            return result
        except json.JSONDecodeError:
            pass

    # Try to parse the whole response as JSON
    try:
        result = json.loads(response)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    json_match = re.search(r"\{[\s\S]*\}", response)
    if json_match:
        try:
            result = json.loads(json_match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Return empty dict if no JSON found
    return {}


def summarize_journal_with_cc(
    content: str,
    entry_date: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Summarize a journal entry using Claude Code.

    Args:
        content: The journal entry content
        entry_date: The date of the entry (for context)
        timeout: Maximum time to wait for response

    Returns:
        Dictionary with extracted summary data:
        - accomplishments: list[str]
        - decisions: list[dict] with topic, decision, rationale
        - blockers: list[dict] with issue, status
        - themes: list[str]
        - work_in_progress: list[str]
        - metrics: dict with commits, prs_created, etc.
    """
    prompt = f"""Analyze this journal entry from {entry_date} and extract structured information.

Return ONLY valid JSON (no explanation) with this exact structure:
{{
    "accomplishments": ["list of things completed or achieved"],
    "decisions": [
        {{"topic": "what the decision was about", "decision": "what was decided", "rationale": "why"}}
    ],
    "blockers": [
        {{"issue": "description of blocker", "status": "active|resolved|deferred"}}
    ],
    "themes": ["main themes or topics worked on"],
    "work_in_progress": ["items started but not finished"],
    "metrics": {{
        "commits": 0,
        "prs_created": 0,
        "prs_merged": 0,
        "issues_created": 0,
        "issues_closed": 0
    }}
}}

Guidelines:
- accomplishments: Clear, actionable items that were completed
- decisions: Important choices made with reasoning
- blockers: Issues preventing progress (active = still blocking)
- themes: High-level topics (e.g., "security", "infrastructure", "documentation")
- metrics: Count from explicit mentions in the journal

Journal Entry:
---
{content}
---

Return ONLY the JSON, no additional text."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    # Ensure all expected keys exist with defaults
    defaults: dict[str, Any] = {
        "accomplishments": [],
        "decisions": [],
        "blockers": [],
        "themes": [],
        "work_in_progress": [],
        "metrics": {
            "commits": 0,
            "prs_created": 0,
            "prs_merged": 0,
            "issues_created": 0,
            "issues_closed": 0,
        },
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_daily_with_cc(
    entries: list[tuple[Path, str]],
    target_date: str,
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Generate a daily summary from multiple journal entries using Claude Code.

    Args:
        entries: List of (filepath, content) tuples for the day's entries
        target_date: The date being summarized
        timeout: Maximum time to wait for response

    Returns:
        Aggregated summary dictionary
    """
    if not entries:
        return {
            "accomplishments": [],
            "decisions": [],
            "blockers": [],
            "themes": [],
            "work_in_progress": [],
            "metrics": {
                "commits": 0,
                "prs_created": 0,
                "prs_merged": 0,
                "issues_created": 0,
                "issues_closed": 0,
            },
        }

    # Combine all entries for the day
    combined_content = "\n\n---\n\n".join(
        [f"### Entry: {filepath.name}\n\n{content}" for filepath, content in entries]
    )

    prompt = f"""Analyze these journal entries from {target_date} and create a unified daily summary.

Return ONLY valid JSON (no explanation) with this structure:
{{
    "accomplishments": ["consolidated list of achievements"],
    "decisions": [
        {{"topic": "topic", "decision": "decision", "rationale": "reason"}}
    ],
    "blockers": [
        {{"issue": "description", "status": "active|resolved|deferred"}}
    ],
    "themes": ["main themes across all entries"],
    "work_in_progress": ["items still in progress"],
    "metrics": {{
        "commits": <total>,
        "prs_created": <total>,
        "prs_merged": <total>,
        "issues_created": <total>,
        "issues_closed": <total>
    }},
    "key_insight": "most important learning or insight from the day"
}}

Guidelines:
- Deduplicate accomplishments across entries
- Consolidate related decisions
- Update blocker statuses (if resolved later in day, mark as resolved)
- Identify overarching themes
- Sum up metrics across all entries
- Extract the single most valuable insight

Journal Entries ({len(entries)} total):
---
{combined_content}
---

Return ONLY the JSON."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    # Ensure defaults
    defaults: dict[str, Any] = {
        "accomplishments": [],
        "decisions": [],
        "blockers": [],
        "themes": [],
        "work_in_progress": [],
        "metrics": {
            "commits": 0,
            "prs_created": 0,
            "prs_merged": 0,
            "issues_created": 0,
            "issues_closed": 0,
        },
        "key_insight": "",
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_weekly_with_cc(
    daily_summaries: list[dict[str, Any]],
    week_id: str,
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Generate a weekly summary from daily summaries using Claude Code.

    Args:
        daily_summaries: List of daily summary dictionaries
        week_id: Week identifier (e.g., "2025-W01")
        timeout: Maximum time to wait for response

    Returns:
        Weekly summary dictionary
    """
    if not daily_summaries:
        return {
            "top_accomplishments": [],
            "key_decisions": [],
            "themes": [],
            "metrics": {
                "commits": 0,
                "prs_created": 0,
                "prs_merged": 0,
                "issues_created": 0,
                "issues_closed": 0,
            },
            "weekly_insight": "",
        }

    # Format daily summaries for context
    summaries_text = "\n\n".join(
        [
            f"### {s.get('date', 'Unknown date')}\n"
            f"Accomplishments: {', '.join(s.get('accomplishments', []))}\n"
            f"Themes: {', '.join(s.get('themes', []))}\n"
            f"Key insight: {s.get('key_insight', 'N/A')}"
            for s in daily_summaries
        ]
    )

    prompt = f"""Synthesize these daily summaries into a weekly summary for {week_id}.

Return ONLY valid JSON with this structure:
{{
    "top_accomplishments": ["top 5-7 most significant achievements"],
    "key_decisions": [
        {{"topic": "topic", "decision": "decision", "impact": "expected impact"}}
    ],
    "themes": ["major themes for the week"],
    "patterns": ["recurring patterns or observations"],
    "metrics": {{
        "commits": <total>,
        "prs_created": <total>,
        "prs_merged": <total>,
        "issues_created": <total>,
        "issues_closed": <total>
    }},
    "weekly_insight": "key learning or strategic insight from the week"
}}

Daily Summaries:
---
{summaries_text}
---

Return ONLY the JSON."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    # Ensure defaults
    defaults: dict[str, Any] = {
        "top_accomplishments": [],
        "key_decisions": [],
        "themes": [],
        "patterns": [],
        "metrics": {
            "commits": 0,
            "prs_created": 0,
            "prs_merged": 0,
            "issues_created": 0,
            "issues_closed": 0,
        },
        "weekly_insight": "",
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_monthly_with_cc(
    weekly_summaries: list[dict[str, Any]],
    month: str,
    timeout: int = 240,
) -> dict[str, Any]:
    """
    Generate a monthly summary from weekly summaries using Claude Code.

    Args:
        weekly_summaries: List of weekly summary dictionaries
        month: Month identifier (e.g., "2025-01")
        timeout: Maximum time to wait for response

    Returns:
        Monthly summary dictionary
    """
    if not weekly_summaries:
        return {
            "major_achievements": [],
            "strategic_decisions": [],
            "monthly_themes": [],
            "capability_growth": [],
            "metrics": {
                "commits": 0,
                "prs_created": 0,
                "prs_merged": 0,
                "issues_created": 0,
                "issues_closed": 0,
            },
            "month_narrative": "",
            "key_learnings": [],
        }

    # Format weekly summaries for context
    summaries_text = "\n\n".join(
        [
            f"### Week {s.get('week', 'Unknown')}\n"
            f"Top accomplishments: {', '.join(s.get('top_accomplishments', s.get('milestones', []))[:5])}\n"
            f"Themes: {', '.join(s.get('themes', s.get('recurring_themes', [])))}\n"
            f"Weekly insight: {s.get('weekly_insight', 'N/A')}"
            for s in weekly_summaries
        ]
    )

    prompt = f"""Synthesize these weekly summaries into a monthly summary for {month}.

Return ONLY valid JSON with this structure:
{{
    "major_achievements": ["top 5-10 most significant achievements for the month"],
    "strategic_decisions": [
        {{"topic": "topic", "decision": "decision", "strategic_impact": "long-term impact"}}
    ],
    "monthly_themes": ["dominant themes across the month"],
    "capability_growth": ["new capabilities or skills developed"],
    "patterns": ["recurring patterns across weeks"],
    "metrics": {{
        "commits": <total>,
        "prs_created": <total>,
        "prs_merged": <total>,
        "issues_created": <total>,
        "issues_closed": <total>
    }},
    "month_narrative": "2-3 sentence narrative summary of the month",
    "key_learnings": ["most important learnings from the month"]
}}

Weekly Summaries:
---
{summaries_text}
---

Return ONLY the JSON."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    # Ensure defaults
    defaults: dict[str, Any] = {
        "major_achievements": [],
        "strategic_decisions": [],
        "monthly_themes": [],
        "capability_growth": [],
        "patterns": [],
        "metrics": {
            "commits": 0,
            "prs_created": 0,
            "prs_merged": 0,
            "issues_created": 0,
            "issues_closed": 0,
        },
        "month_narrative": "",
        "key_learnings": [],
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result
