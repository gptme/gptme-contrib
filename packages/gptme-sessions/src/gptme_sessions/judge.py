"""LLM-as-judge for session goal-alignment scoring.

Evaluates agent sessions for strategic value using a cheap LLM (Haiku).
Returns a score (0.0-1.0) and a 1-sentence reason.

The judge is designed to be:
- **Generic**: Works for any agent, not just Bob. Goals are passed as a parameter.
- **Cheap**: Uses Haiku (~$0.001/eval) so it can run on every session.
- **Optional**: Requires ``anthropic`` package. Returns None on failure.

Integration points:
- ``gptme-sessions signals --llm-judge``: score a single trajectory
- ``gptme-sessions judge``: batch-score sessions from journal entries
- ``post_session()``: can be called after signal extraction
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM = """\
You are evaluating an AI agent's work session for strategic value.
Return ONLY a JSON object with two keys: "score" (float 0.0-1.0) and "reason" (string, 1 sentence).
Do not wrap in markdown code blocks."""

JUDGE_PROMPT_TEMPLATE = """\
## Agent Goals (ordered by priority)
{goals}

## Session Category
{category}

## Session Journal
{journal}

## Scoring Rubric
Rate the strategic value of this session's output on 0.0-1.0:
- 0.9-1.0: Major progress on a top-priority goal (shipped key feature, unblocked critical path)
- 0.7-0.8: Meaningful progress on a priority goal (solid PR, design doc, real bug fix)
- 0.5-0.6: Useful but not top-priority work (minor improvements, maintenance)
- 0.3-0.4: Low-value work (tiny PRs, busywork, over-engineering)
- 0.1-0.2: Minimal output (mostly blocked, NOOP-adjacent)
- 0.0: No output / pure NOOP

Key principle: ONE impactful deliverable > FIVE small deliverables.
Evaluate impact and goal-alignment, not quantity.

Return JSON: {{"score": <float>, "reason": "<1 sentence>"}}"""

DEFAULT_GOALS = """\
The agent is a general-purpose autonomous AI assistant.
1. Complete assigned tasks effectively
2. Write high-quality code and documentation
3. Self-improve through lessons and patterns
4. Contribute to open-source projects"""


def _get_api_key() -> str:
    """Resolve Anthropic API key from environment or gptme config."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # Try gptme config as fallback
    try:
        import tomllib

        config_path = Path.home() / ".config" / "gptme" / "config.toml"
        if config_path.exists():
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            key = config.get("env", {}).get("ANTHROPIC_API_KEY", "")
    except Exception:
        pass
    return key


def judge_session(
    journal_text: str,
    category: str | None = None,
    *,
    goals: str = DEFAULT_GOALS,
    model: str = DEFAULT_JUDGE_MODEL,
    api_key: str | None = None,
) -> dict | None:
    """Score a session's strategic value using an LLM judge.

    Args:
        journal_text: The session journal/summary text to evaluate.
        category: Work category (e.g. "code", "triage", "content").
        goals: Agent goals description (ordered by priority).
        model: Anthropic model ID for the judge.
        api_key: Anthropic API key. Falls back to env/config if not provided.

    Returns:
        Dict with keys ``score`` (float), ``reason`` (str), ``model`` (str),
        or ``None`` if the evaluation fails (missing API key, import error, etc.).
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; LLM judge unavailable")
        return None

    key = api_key or _get_api_key()
    if not key:
        logger.warning("No Anthropic API key found; LLM judge unavailable")
        return None

    # Truncate journal to ~4000 chars to keep costs low
    truncated = journal_text[:4000] if journal_text else "(empty session)"

    # str.format() only parses {placeholder} in the template itself — values
    # passed as keyword arguments are substituted verbatim without re-parsing.
    # No escaping of {/} in user content is needed or correct.
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        goals=goals,
        category=category or "unknown",
        journal=truncated,
    )

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=150,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = getattr(response.content[0], "text", "").strip()

        # Handle markdown code block wrapping — use regex to avoid splitting on
        # backticks that appear inside the reason string
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        verdict = json.loads(text)
        score = float(verdict.get("score", 0.5))
        reason = str(verdict.get("reason", ""))

        # Clamp to valid range
        score = max(0.0, min(1.0, score))

        return {"score": score, "reason": reason, "model": model}

    except Exception as e:
        logger.warning("LLM judge failed: %s", e)
        return None


def judge_from_signals(
    signals: dict,
    journal_text: str | None = None,
    category: str | None = None,
    **kwargs,
) -> dict | None:
    """Score a session using extracted signals as context.

    If ``journal_text`` is not provided, a summary is synthesized from
    the signals dict (commits, file writes, tool calls, etc.).

    All extra kwargs are forwarded to :func:`judge_session`.
    """
    if not journal_text:
        # Synthesize a summary from signals
        parts = []
        commits = signals.get("git_commits", [])
        if commits:
            parts.append(f"Git commits ({len(commits)}):")
            for c in commits[:10]:
                parts.append(f"  - {c}")
        writes = signals.get("file_writes", [])
        if writes:
            unique = list(dict.fromkeys(writes))
            parts.append(f"Files written ({len(unique)} unique):")
            for w in unique[:10]:
                parts.append(f"  - {w}")
        tools = signals.get("tool_calls", {})
        if tools:
            total = sum(tools.values())
            top5 = sorted(tools.items(), key=lambda x: -x[1])[:5]
            parts.append(f"Tool calls: {total} total ({', '.join(f'{t}:{n}' for t, n in top5)})")
        grade = signals.get("grade")
        if grade is not None:
            parts.append(f"Trajectory grade: {grade:.2f}")
        errors = signals.get("error_count", 0)
        if errors:
            parts.append(f"Errors: {errors}")
        duration = signals.get("session_duration_s", 0)
        if duration:
            parts.append(f"Duration: {duration}s")

        journal_text = "\n".join(parts) if parts else "(no signal data)"

    return judge_session(journal_text, category=category, **kwargs)
