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
    # Allow nesting: unset CLAUDECODE to avoid the nested-session guard
    # when called from within a Claude Code session (e.g. systemd timer).
    import os

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    result = subprocess.run(
        ["claude", "-p", "-"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
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
    extra_context: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Summarize a journal entry using Claude Code.

    Args:
        content: The journal entry content
        entry_date: The date of the entry (for context)
        extra_context: Additional context (GitHub activity, session data) to inject
        timeout: Maximum time to wait for response

    Returns:
        Dictionary with extracted summary data
    """
    context_section = ""
    if extra_context:
        context_section = f"""
The following real data is available for reference. DO NOT count or estimate metrics — those are provided separately.

{extra_context}
"""

    prompt = f"""Analyze this journal entry from {entry_date} and extract structured information.
{context_section}
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
    "narrative": "2-3 sentence prose summary of the entry",
    "interactions": [
        {{"type": "github_review|github_issue|social_post|conversation|discord", "person": "who", "summary": "what happened", "url": ""}}
    ],
    "external_signals": [
        {{"source": "RSS|Hacker News|Twitter|journal|podcast", "title": "headline", "relevance": "why it matters", "url": ""}}
    ]
}}

Guidelines:
- accomplishments: Clear, actionable items that were completed
- decisions: Important choices made with reasoning
- blockers: Issues preventing progress (active = still blocking)
- themes: High-level topics (e.g., "security", "infrastructure", "documentation")
- narrative: A concise prose summary capturing the essence of the day's work
- interactions: Human/social interactions — PR reviews, conversations with Erik, social media posts, Discord chats
- external_signals: News, model releases, ecosystem developments, research findings, podcast mentions — anything from outside that informs strategy
- Reference PRs and issues using full owner/repo#NNN format (e.g. gptme/gptme#1265) so they can be hyperlinked
- DO NOT include a "metrics" field — metrics are tracked separately from real data

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
        "narrative": "",
        "interactions": [],
        "external_signals": [],
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_daily_with_cc(
    entries: list[tuple[Path, str]],
    target_date: str,
    extra_context: str = "",
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Generate a daily summary from multiple journal entries using Claude Code.

    Args:
        entries: List of (filepath, content) tuples for the day's entries
        target_date: The date being summarized
        extra_context: Additional context (GitHub activity, session data) to inject
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
            "narrative": "",
        }

    # Combine all entries for the day
    combined_content = "\n\n---\n\n".join(
        [f"### Entry: {filepath.name}\n\n{content}" for filepath, content in entries]
    )

    context_section = ""
    if extra_context:
        context_section = f"""
The following real data is available for reference. DO NOT count or estimate metrics — those are provided separately.

{extra_context}
"""

    prompt = f"""Analyze these journal entries from {target_date} and create a unified daily summary.
{context_section}
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
    "narrative": "2-3 sentence prose summary of the day's work",
    "key_insight": "most important learning or insight from the day",
    "interactions": [
        {{"type": "github_review|github_issue|social_post|conversation|discord", "person": "who", "summary": "what happened", "url": ""}}
    ],
    "external_signals": [
        {{"source": "RSS|Hacker News|Twitter|journal|podcast", "title": "headline", "relevance": "why it matters", "url": ""}}
    ]
}}

Guidelines:
- Deduplicate accomplishments across entries
- Consolidate related decisions
- Update blocker statuses (if resolved later in day, mark as resolved)
- Identify overarching themes
- Extract the single most valuable insight
- interactions: Human/social interactions — PR reviews, conversations with Erik, social media posts, Discord chats
- external_signals: News, model releases, ecosystem developments, research findings, podcast mentions
- Reference PRs and issues using full owner/repo#NNN format (e.g. gptme/gptme#1265) so they can be hyperlinked
- DO NOT include a "metrics" field — metrics are tracked separately from real data

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
        "narrative": "",
        "key_insight": "",
        "interactions": [],
        "external_signals": [],
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_weekly_with_cc(
    daily_summaries: list[dict[str, Any]],
    week_id: str,
    extra_context: str = "",
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Generate a weekly summary from daily summaries using Claude Code.

    Args:
        daily_summaries: List of daily summary dictionaries
        week_id: Week identifier (e.g., "2025-W01")
        extra_context: Additional context (GitHub activity, session data) to inject
        timeout: Maximum time to wait for response

    Returns:
        Weekly summary dictionary
    """
    if not daily_summaries:
        return {
            "top_accomplishments": [],
            "key_decisions": [],
            "themes": [],
            "narrative": "",
            "weekly_insight": "",
        }

    # Format daily summaries with full context (decisions, blockers, WIP — not just accomplishments+themes)
    summaries_text = "\n\n".join(
        [
            f"### {s.get('date', 'Unknown date')}\n"
            f"Accomplishments: {', '.join(s.get('accomplishments', []))}\n"
            f"Decisions: {json.dumps(s.get('decisions', []))}\n"
            f"Blockers: {json.dumps(s.get('blockers', []))}\n"
            f"Work in progress: {', '.join(s.get('work_in_progress', []))}\n"
            f"Themes: {', '.join(s.get('themes', []))}\n"
            f"Key insight: {s.get('key_insight', s.get('narrative', 'N/A'))}"
            for s in daily_summaries
        ]
    )

    context_section = ""
    if extra_context:
        context_section = f"""
The following real data is available for reference. DO NOT count or estimate metrics — those are provided separately.

{extra_context}
"""

    prompt = f"""Synthesize these daily summaries into a weekly summary for {week_id}.
{context_section}
Return ONLY valid JSON with this structure:
{{
    "top_accomplishments": ["top 5-7 most significant achievements"],
    "key_decisions": [
        {{"topic": "topic", "decision": "decision", "impact": "expected impact"}}
    ],
    "themes": ["major themes for the week"],
    "patterns": ["recurring patterns or observations"],
    "narrative": "2-3 sentence prose summary of the week",
    "weekly_insight": "key learning or strategic insight from the week"
}}

Guidelines:
- Focus on impact and significance, not just listing everything
- Identify patterns across days
- Reference PRs and issues using full owner/repo#NNN format (e.g. gptme/gptme#1265) so they can be hyperlinked
- DO NOT include a "metrics" field — metrics are tracked separately from real data

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
        "narrative": "",
        "weekly_insight": "",
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_github_activity_with_cc(
    github_context: str,
    username: str,
    period: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Summarize GitHub activity for a human user using Claude Code.

    Args:
        github_context: Formatted GitHub activity markdown
        username: GitHub username
        period: Human-readable period description (e.g., "2026-02-10 to 2026-02-17")
        timeout: Maximum time to wait for response

    Returns:
        Dictionary with structured activity summary
    """
    prompt = f"""Analyze this GitHub activity for user @{username} during {period} and create a structured summary.

{github_context}

Return ONLY valid JSON (no explanation) with this structure:
{{
    "highlights": ["top 3-5 most notable contributions or achievements"],
    "projects_active": [
        {{"repo": "owner/name", "description": "what they worked on", "prs": 0, "issues": 0}}
    ],
    "themes": ["main areas of focus"],
    "narrative": "2-3 sentence summary of the person's GitHub activity during this period",
    "stats": {{
        "total_prs": 0,
        "total_issues": 0,
        "total_commits": 0,
        "repos_active": 0
    }}
}}

Guidelines:
- Focus on what the person accomplished, not internal tool/agent details
- Identify themes and patterns in their work
- Keep the narrative human-readable and suitable for a profile or newsletter
- Group related work into coherent project descriptions

Return ONLY the JSON."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    defaults: dict[str, Any] = {
        "highlights": [],
        "projects_active": [],
        "themes": [],
        "narrative": "",
        "stats": {
            "total_prs": 0,
            "total_issues": 0,
            "total_commits": 0,
            "repos_active": 0,
        },
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_human_day_with_cc(
    context: str,
    username: str,
    day: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Summarize a human's day using multi-source context (AW + GitHub).

    Args:
        context: Combined context string (AW time tracking + GitHub activity)
        username: Username or display name for the person
        day: Date string (YYYY-MM-DD)
        timeout: Maximum time to wait for response

    Returns:
        Dictionary with structured daily summary
    """
    prompt = f"""Analyze this activity data for {username} on {day} and create a daily summary.

{context}

Return ONLY valid JSON (no explanation) with this structure:
{{
    "narrative": "2-3 sentence summary of what {username} worked on today",
    "highlights": ["top 3-5 most notable activities or achievements"],
    "time_breakdown": ["Category: Xh (description)", ...],
    "themes": ["main focus areas or themes for the day"]
}}

Guidelines:
- Focus on what the person worked on and accomplished
- For time tracking data, identify categories (coding, communication, browsing, etc.)
- Keep the narrative concise and human-readable
- If both time tracking and GitHub data are present, synthesize them together

Return ONLY the JSON."""

    response = call_claude_code(prompt, timeout=timeout)
    result = extract_json_from_response(response)

    defaults: dict[str, Any] = {
        "narrative": "",
        "highlights": [],
        "time_breakdown": [],
        "themes": [],
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


def summarize_monthly_with_cc(
    weekly_summaries: list[dict[str, Any]],
    month: str,
    extra_context: str = "",
    timeout: int = 240,
) -> dict[str, Any]:
    """
    Generate a monthly summary from weekly summaries using Claude Code.

    Args:
        weekly_summaries: List of weekly summary dictionaries
        month: Month identifier (e.g., "2025-01")
        extra_context: Additional context (GitHub activity, session data) to inject
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
            "month_narrative": "",
            "key_learnings": [],
        }

    # Format weekly summaries with full context (milestones, decisions, trends, patterns)
    summaries_text = "\n\n".join(
        [
            f"### Week {s.get('week', 'Unknown')}\n"
            f"Top accomplishments: {', '.join(s.get('top_accomplishments', s.get('milestones', []))[:7])}\n"
            f"Key decisions: {json.dumps(s.get('key_decisions', []))}\n"
            f"Themes: {', '.join(s.get('themes', s.get('recurring_themes', [])))}\n"
            f"Patterns: {', '.join(s.get('patterns', s.get('trends', [])))}\n"
            f"Narrative: {s.get('narrative', s.get('weekly_insight', 'N/A'))}"
            for s in weekly_summaries
        ]
    )

    context_section = ""
    if extra_context:
        context_section = f"""
The following real data is available for reference. DO NOT count or estimate metrics — those are provided separately.

{extra_context}
"""

    prompt = f"""Synthesize these weekly summaries into a monthly summary for {month}.
{context_section}
Return ONLY valid JSON with this structure:
{{
    "major_achievements": ["top 5-10 most significant achievements for the month"],
    "strategic_decisions": [
        {{"topic": "topic", "decision": "decision", "strategic_impact": "long-term impact"}}
    ],
    "monthly_themes": ["dominant themes across the month"],
    "capability_growth": ["new capabilities or skills developed"],
    "patterns": ["recurring patterns across weeks"],
    "month_narrative": "2-3 sentence narrative summary of the month",
    "key_learnings": ["most important learnings from the month"]
}}

Guidelines:
- Focus on strategic significance and long-term impact
- Identify capability growth and learning arcs
- Reference PRs and issues using full owner/repo#NNN format (e.g. gptme/gptme#1265) so they can be hyperlinked
- DO NOT include a "metrics" field — metrics are tracked separately from real data

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
        "month_narrative": "",
        "key_learnings": [],
    }

    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result
