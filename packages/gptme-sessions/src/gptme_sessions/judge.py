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

import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

from .store import SessionStore

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"
CONFIG_PATHS = (
    Path.home() / ".config" / "gptme" / "config.toml",
    Path.home() / ".config" / "gptme" / "config.local.toml",
)
CONTEXT_FALLBACKS = {
    "JUDGE": ("JUDGE", "LLM_JUDGE", "EVAL"),
    "LLM_JUDGE": ("LLM_JUDGE", "JUDGE", "EVAL"),
    "EVAL": ("EVAL", "JUDGE", "LLM_JUDGE"),
}


class JudgeMetadata(TypedDict):
    backend: str
    judge_version: str


class JudgeVerdict(TypedDict, total=False):
    score: float
    reason: str
    model: str
    meta: JudgeMetadata


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
NO_THINK_PREFILL = "<think></think>\n"
NO_THINK_PREFILL_MODELS = frozenset({"lmstudio/qwen/qwen3.6-35b-a3b"})


def _compute_judge_version() -> str:
    payload = "\n---\n".join(
        [
            JUDGE_SYSTEM.strip(),
            JUDGE_PROMPT_TEMPLATE.strip(),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"goal-alignment-v1-{digest}"


JUDGE_VERSION = _compute_judge_version()


def _get_api_key(config_paths: tuple[Path, ...] = CONFIG_PATHS) -> str:
    """Resolve Anthropic API key from environment or gptme config."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # Try gptme config as fallback (reads both config.toml and config.local.toml)
    return _load_config_env(config_paths).get("ANTHROPIC_API_KEY", "")


def _normalize_context(context: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", context.strip().upper()).strip("_")


def _candidate_openrouter_env_var_names(context: str) -> list[str]:
    normalized = _normalize_context(context)
    names: list[str] = []
    for candidate in CONTEXT_FALLBACKS.get(normalized, ((normalized,) if normalized else ())):
        env_var = f"OPENROUTER_API_KEY_{candidate}"
        if env_var not in names:
            names.append(env_var)
    names.append("OPENROUTER_API_KEY")
    return names


def _load_config_env(config_paths: tuple[Path, ...] = CONFIG_PATHS) -> dict[str, str]:
    """Load merged [env] values from gptme config, with local overrides last."""
    merged: dict[str, str] = {}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return merged

    for path in config_paths:
        if not path.exists():
            continue
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception:
            continue
        env = data.get("env", {})
        for key, value in env.items():
            if isinstance(value, str):
                merged[key] = value
    return merged


def _resolve_openrouter_api_key(
    context: str,
    *,
    environ: dict[str, str] | None = None,
    config_paths: tuple[Path, ...] = CONFIG_PATHS,
) -> str | None:
    """Resolve a scoped OpenRouter key, falling back to the shared default."""
    env = os.environ if environ is None else environ
    candidates = _candidate_openrouter_env_var_names(context)

    for name in candidates:
        value = env.get(name, "")
        if value:
            return value

    config_env = _load_config_env(config_paths)
    for name in candidates:
        value = config_env.get(name, "")
        if value:
            return value

    return None


@contextlib.contextmanager
def _judge_openrouter_env(model: str) -> Iterator[None]:
    """Temporarily promote the judge-scoped OpenRouter key for judge calls."""
    if not model.startswith("openrouter/"):
        yield
        return
    judge_key = _resolve_openrouter_api_key("JUDGE")
    if not judge_key:
        yield
        return
    backup = os.environ.get("OPENROUTER_API_KEY")
    if backup == judge_key:
        yield
        return
    os.environ["OPENROUTER_API_KEY"] = judge_key
    try:
        yield
    finally:
        if backup is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = backup


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


def _judge_backend(model: str) -> str:
    if _is_anthropic_direct_model(model):
        return "anthropic-direct"
    return "gptme-fallback"


def _build_judge_meta(*, model: str) -> JudgeMetadata:
    return {
        "backend": _judge_backend(model),
        "judge_version": JUDGE_VERSION,
    }


def normalize_judge_verdict(payload: dict[str, Any]) -> JudgeVerdict:
    """Attach stable metadata to a raw judge verdict."""
    model = str(payload.get("model", ""))
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    base_meta = _build_judge_meta(model=model)
    return {
        "score": max(0.0, min(1.0, float(payload.get("score", 0.5)))),
        "reason": str(payload.get("reason", "")),
        "model": model,
        "meta": {
            "backend": str(meta.get("backend", base_meta["backend"])),
            "judge_version": str(meta.get("judge_version", base_meta["judge_version"])),
        },
    }


def _strip_json_wrappers(text: str) -> str:
    """Normalize common LLM wrappers around a JSON payload."""
    cleaned = text.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()

    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()

    return cleaned


def _parse_judge_payload(text: str, model: str) -> dict | None:
    """Parse a judge JSON response, tolerating markdown code fences."""
    if not text:
        return None
    payload = _strip_json_wrappers(text)
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", payload)
        if not match:
            logger.warning("LLM judge returned non-JSON response")
            return None
        verdict = json.loads(match.group(0))
    score = float(verdict.get("score", 0.5))
    reason = str(verdict.get("reason", ""))
    score = max(0.0, min(1.0, score))
    return {"score": score, "reason": reason, "model": model}


def _prepare_messages_for_model(messages: list[Any], model: str) -> list[Any]:
    """Apply known model-specific message tweaks before a gptme judge call."""
    prepared = list(messages)
    if model not in NO_THINK_PREFILL_MODELS:
        return prepared
    if (
        prepared
        and getattr(prepared[-1], "role", None) == "assistant"
        and getattr(prepared[-1], "content", None) == NO_THINK_PREFILL
    ):
        return prepared

    from gptme.message import Message

    prepared.append(Message("assistant", NO_THINK_PREFILL))
    return prepared


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
        with _judge_openrouter_env(model):
            init_gptme(
                model=model,
                interactive=False,
                tool_allowlist=[],
                tool_format="markdown",
            )
            response = reply(
                _prepare_messages_for_model(
                    [
                        Message("system", JUDGE_SYSTEM),
                        Message("user", prompt),
                    ],
                    model,
                ),
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


def judge_session_with_fallback(
    journal_text: str,
    category: str | None = None,
    *,
    goals: str = DEFAULT_GOALS,
    default_model: str = DEFAULT_JUDGE_MODEL,
    fallback_models: tuple[str, ...] = (),
    api_key: str | None = None,
) -> dict | None:
    """Score a session, trying fallback models if the primary is unavailable.

    Args:
        journal_text: The session journal/summary text to evaluate.
        category: Work category (e.g. "code", "triage", "content").
        goals: Agent goals description (ordered by priority).
        default_model: Primary judge model. Tried first.
        fallback_models: Additional models to try in order when the primary fails.
        api_key: Anthropic API key (Anthropic-direct path only).

    Returns:
        Dict with keys ``score``, ``reason``, ``model`` or ``None`` if all models fail.
    """
    models_to_try = (default_model, *fallback_models)
    for i, model in enumerate(models_to_try):
        if i > 0:
            logger.info(
                "Judge model %s unavailable; falling back to %s",
                models_to_try[i - 1],
                model,
            )
        result = judge_session(
            journal_text,
            category=category,
            goals=goals,
            model=model,
            api_key=api_key,
        )
        if result is not None:
            return result
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


def _store_judge_meta(record: Any, meta: JudgeMetadata | None) -> None:
    """Persist judge metadata via the SessionRecord legacy-field bridge."""
    if not meta:
        return
    normalized = {k: str(v) for k, v in meta.items() if v}
    if not normalized:
        return
    legacy_fields = getattr(record, "_legacy_fields", None)
    if isinstance(legacy_fields, dict):
        legacy_fields["llm_judge_meta"] = normalized


def write_alignment_grade(
    *,
    session_id: str,
    verdict: JudgeVerdict,
    sessions_dir: Path,
) -> bool:
    """Persist an alignment verdict onto an existing session record."""
    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    normalized = normalize_judge_verdict(dict(verdict))
    for record in records:
        if record.session_id != session_id:
            continue
        record.set_alignment_grade(
            normalized["score"],
            reason=normalized["reason"],
            model=normalized["model"],
        )
        _store_judge_meta(record, normalized.get("meta"))
        store.rewrite(records)
        return True
    return False


def judge_and_writeback(
    *,
    text: str,
    category: str | None,
    goals: str,
    session_id: str,
    sessions_dir: Path,
    model: str = DEFAULT_JUDGE_MODEL,
    fallback_models: tuple[str, ...] = (),
    api_key: str | None = None,
) -> dict[str, Any]:
    """Judge a session and persist the verdict via SessionStore.

    If ``fallback_models`` is provided, tries them in order when ``model`` fails.
    """
    if fallback_models:
        verdict = judge_session_with_fallback(
            text,
            category=category,
            goals=goals,
            default_model=model,
            fallback_models=fallback_models,
            api_key=api_key,
        )
    else:
        verdict = judge_session(
            text,
            category=category,
            goals=goals,
            model=model,
            api_key=api_key,
        )
    if verdict is None:
        return {"status": "failed"}

    normalized = normalize_judge_verdict(verdict)
    updated = write_alignment_grade(
        session_id=session_id,
        verdict=normalized,
        sessions_dir=sessions_dir,
    )
    if not updated:
        return {"status": "no_record", **normalized}

    return {"status": "ok", **normalized}
