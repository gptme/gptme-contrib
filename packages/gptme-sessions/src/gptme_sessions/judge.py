"""LLM-as-judge for session goal-alignment scoring.

Evaluates agent sessions for strategic value using an LLM.
Returns a score (0.0-1.0) and a 1-sentence reason.

The judge is designed to be:
- **Generic**: Works for any agent, not just Bob. Goals are passed as a parameter.
- **Cheap**: Uses Haiku by default (~$0.001/eval).
- **Pluggable**: ``--model`` supports direct Anthropic IDs *and* any provider
  gptme can route to (``openrouter/...``, ``openai-subscription/...``,
  ``lmstudio/...``, ``anthropic/...``, etc.) when the ``gptme`` package is
  installed.
- **Optional**: Returns None on failure (missing API key, import error).

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


def _is_anthropic_direct_model(model: str) -> bool:
    """True if the model ID should route via the direct Anthropic SDK.

    Accepts bare Anthropic IDs (``claude-haiku-4-5-...``) and the
    ``anthropic/<model>`` prefix. Anything else (``openrouter/...``,
    ``openai-subscription/...``, ``lmstudio/...``, ``openai/...``, etc.)
    routes through ``gptme.llm`` when available.
    """
    if model.startswith("anthropic/"):
        return True
    # Bare model IDs without a provider prefix: treat as Anthropic-direct iff
    # they look like Anthropic IDs (keeps backcompat with the legacy default).
    if "/" not in model and model.startswith("claude-"):
        return True
    return False


def _strip_anthropic_prefix(model: str) -> str:
    return model.removeprefix("anthropic/") if model.startswith("anthropic/") else model


def _parse_judge_payload(text: str, model: str) -> dict | None:
    """Parse a judge JSON response, tolerating markdown code fences."""
    if not text:
        return None
    payload = text.strip()
    if "```" in payload:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", payload, re.DOTALL)
        if m:
            payload = m.group(1).strip()
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("LLM judge returned non-JSON response: %s", exc)
        return None
    score = float(verdict.get("score", 0.5))
    reason = str(verdict.get("reason", ""))
    score = max(0.0, min(1.0, score))
    return {"score": score, "reason": reason, "model": model}


def _judge_via_anthropic_direct(
    prompt: str,
    *,
    model: str,
    api_key: str | None,
) -> dict | None:
    """Call the judge via the direct Anthropic SDK (legacy path)."""
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; direct judge unavailable")
        return None

    key = api_key or _get_api_key()
    if not key:
        logger.warning("No Anthropic API key found; direct judge unavailable")
        return None

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=_strip_anthropic_prefix(model),
            max_tokens=150,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = getattr(response.content[0], "text", "").strip()
    except Exception as exc:
        logger.warning("LLM judge (anthropic) failed: %s", exc)
        return None

    return _parse_judge_payload(text, model)


def _judge_via_gptme(prompt: str, *, model: str) -> dict | None:
    """Call the judge via ``gptme.llm.reply`` for non-Anthropic-direct models."""
    try:
        from gptme.init import init as init_gptme
        from gptme.llm import reply
        from gptme.message import Message
    except ImportError:
        logger.warning(
            "gptme package not installed; model %r needs gptme.llm routing "
            "(install gptme, or use an Anthropic-direct model ID)",
            model,
        )
        return None

    try:
        init_gptme(
            model=model,
            interactive=False,
            tool_allowlist=[],
            tool_format="markdown",
        )
        response = reply(
            [
                Message("system", JUDGE_SYSTEM),
                Message("user", prompt),
            ],
            model,
            stream=False,
        )
    except Exception as exc:
        logger.warning("LLM judge (gptme) failed: %s", exc)
        return None

    text = getattr(response, "content", "")
    return _parse_judge_payload(text, model)


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
        model: Model ID for the judge. Anthropic-direct IDs
            (``claude-*``, ``anthropic/*``) use the ``anthropic`` SDK.
            Other provider-prefixed IDs (``openrouter/...``,
            ``openai-subscription/...``, ``lmstudio/...``, etc.) route
            through ``gptme.llm.reply`` when the ``gptme`` package is
            installed.
        api_key: Anthropic API key (Anthropic-direct path only). Falls
            back to env/config if not provided.

    Returns:
        Dict with keys ``score`` (float), ``reason`` (str), ``model`` (str),
        or ``None`` if the evaluation fails (missing API key, import error,
        non-JSON response, etc.).
    """
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

    if _is_anthropic_direct_model(model):
        return _judge_via_anthropic_direct(prompt, model=model, api_key=api_key)
    return _judge_via_gptme(prompt, model=model)


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
            try:
                parts.append(f"Trajectory grade: {float(grade):.2f}")
            except (ValueError, TypeError):
                pass
        errors = signals.get("error_count", 0)
        if errors:
            parts.append(f"Errors: {errors}")
        duration = signals.get("session_duration_s", 0)
        if duration:
            parts.append(f"Duration: {duration}s")

        journal_text = "\n".join(parts) if parts else "(no signal data)"

    return judge_session(journal_text, category=category, **kwargs)
