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
    alignment_score: float | None
    pivot_verdict: str | None
    meta: JudgeMetadata


JUDGE_SYSTEM = """\
You are evaluating an AI agent's work session for strategic value.
Return ONLY a JSON object with two keys: "score" (float 0.0-1.0) and "reason" (string, 1 sentence).
Do not wrap in markdown code blocks.
IMPORTANT: Use the FULL 0.0-1.0 range. Do not cluster scores at 0.7-0.8. Reserve 0.9+ for exceptional sessions; assign 0.1-0.3 for minimal or blocked sessions."""

JUDGE_PROMPT_TEMPLATE = """\
## Session Category
{category}
{routing_context}
{intent_context}
## Session Journal
{journal}

## Scoring Rubric
Rate the strategic value of this session's output on 0.0-1.0:
- 0.9-1.0: Major progress on a top-priority goal (shipped key feature, unblocked critical path)
- 0.7-0.8: Meaningful progress on any goal — priority or compounding support (solid PR, durable infra win, real bug fix)
- 0.5-0.6: Low-impact output in any category (weak improvements, thrashing-lite)
- 0.3-0.4: Low-value work (tiny PRs, busywork, over-engineering)
- 0.1-0.2: Minimal output (mostly blocked, NOOP-adjacent)
- 0.0: No output / pure NOOP

## Category Interpretation
Sessions are routed across categories. Some categories (code, content,
cross-repo) directly target top-priority product/revenue goals. Others
(infrastructure, monitoring, cleanup, knowledge, research, self-review,
triage, news, social, novelty, strategic) are support work that compounds
future capability. Score on **within-category value**, not on whether the
category itself targets goal #1:

- A clean infrastructure / instrumentation / measurement session that
  meaningfully reduces future friction or persists durable learning is
  goal-aligned — score 0.7-0.9, do not cap it for "not revenue work".
- A well-executed research, knowledge, or strategic session that yields
  genuine insight or a durable artifact is goal-aligned — score 0.7-0.9,
  do not cap it for "not revenue work".
- Penalize all categories equally for thrashing, no-shipped-artifact,
  over-engineering, or work the agent invented to fill the session.
- Reserve 0.9-1.0 for sessions where the deliverable plausibly moves a
  top-priority goal *or* is a category-best example of compounding work.

Key principle: ONE impactful deliverable > FIVE small deliverables.

## Agent Goals (ordered by priority)
{goals}

## Worked Examples
The following worked examples show how the rubric applies to real session
shapes, explaining *why* specific categories earn their score range:

**Example 1 (infrastructure, ~0.78)**: A session normalizes a flaky CI health
check that has been producing false alerts for a week. It discovers the stale
worker accumulation, removes the redundant probes, and adds a regression test.
This session scores ~0.78 because:
- Infrastructure compounding: every future session inherits a cleaner CI signal.
- The artifact (test + config fix) is self-verifying and durable.
- The work is not "revenue work" — that is irrelevant. The within-category
  value is high: it permanently reduced future friction.

**Example 2 (cleanup, ~0.75)**: A session audits lesson keywords using the LOO
analysis tool, removes 8 dead keywords whose delta was harmfully negative, and
updates a companion doc with the audit rationale. This session scores ~0.75
because:
- The removed keywords measurably improved agent behavior (validated by
  LOO effect-size delta).
- The artifact (keyword changes + companion doc) is durable — every future
  agent session benefits without extra effort.
- Busywork-versus-value boundary: the session only removed *harmful* keywords,
  not all dead weight. It stopped at the value line.

**Example 3 (research, ~0.72)**: A session reads an arXiv paper on tool-use
reliability, writes a structured research note connecting the findings to the
agent's architecture, and files a targeted idea-backlog entry. This session
scores ~0.72 because:
- The research note is a durable artifact that future sessions reference.
- It connects abstract findings to concrete, actionable next steps (e.g.,
  "try two-pass prompting on high-stakes turns").
- It does *not* score higher because it produced no code change, no test, no
  automated verification — the impact is mediated through future sessions'
  ability to use the insight.

**Example 4 (code, ~0.92)**: A session ships a new subagent cancellation API
(``subagent_cancel()``), adds 12 unit tests covering normal completion and
timeout, gets Greptile 5/5, and merges cleanly. This session scores ~0.92 because:
- It directly advances a top-priority goal (subagent reliability, a P1 feature).
- The artifact is self-verifying (tests), durable (merged), and has no rework.
- 0.92 rather than 1.0 because no end-to-end integration test was added.

**Example 5 (infrastructure, ~0.15)**: A session starts planning a memory
cleanup, but every file it tries to modify is locked by a parallel session's
git stash. It re-checks task state, finds the work already done, writes a
one-line journal entry, and ends. This session scores ~0.15 because:
- No artifact was produced — the work was already done by someone else.
- The only output is a journal entry acknowledging the NOOP.
- 0.15 rather than 0.0 because the loose-end check and duplicate detection
  were correct agent behavior — pure NOOP would be 0.0.

**Example 6 (content, ~0.75)**: A session writes, publishes, and promotes a
technical blog post documenting a real feature with reproduction steps, code
examples, and published-artifact links. The post is published on the agent's
blog with an OG image, tweeted from the project account, and linked in the
relevant GitHub issue. This session scores ~0.75 because:
- The blog post is a durable, publicly-verifiable artifact that compounds
  awareness and documents real shipped work.
- It ships end-to-end (draft editor → publish → social promotion) with no
  incomplete steps or broken links.
- It does *not* score higher because the impact is indirect (awareness/
  knowledge) rather than direct product progress or revenue impact.
- The "within-category" principle applies: this is high-quality content
  execution, and the content priority tier is irrelevant — the same
  execution standard used for infrastructure, cleanup, and research
  examples applies here. Do not penalize content sessions for being
  "priority #6" or "lowest priority".

Key principle across all six: the grade follows from *within-category*
execution quality, compounding value, and durability — not from whether the
category number matches a top goal. Use scores below 0.5 when output is
minimal, blocked, or duplicated, and scores above 0.85 only for sessions
that ship a durable, high-impact deliverable.

## Forbidden Constructions
Do NOT invoke any of the following as a penalty rationale:
- "low priority", "Tier 3", "Tier 5", "not a top goal"
- "misaligned with top goals" or "misaligned with top-priority"
- "not revenue-generating" or "not revenue work"
- "below the top revenue-generating goal"
- "fallback tier" or "lower-tier work" (in a negative sense)

These phrases are category signals, NOT quality signals. Penalize only
on actual execution quality: thrashing, no-shipped-artifact, over-engineering,
or invented work — not on what tier or priority the category occupies.

## Routing-Aware Adjustment
If a `## Routing Context` block above states the session legitimately
routed to lower-tier work because higher-tier work was unavailable
(blocked / waiting / no actionable items), score primarily on execution
quality within the chosen scope. Do not penalize for non-advancement of
top-priority goals beyond −0.15 below the execution-quality score: the
selector — not the agent — decided what was eligible.
If no `## Routing Context` block is present, ignore this adjustment and
apply the default rubric.

## Intent→Outcome Alignment
When a `## Session Intent` block is present above, separately rate how well
the session's actual output matched its declared intent. This is a *separate*
axis from the execution-quality score — a good pivot still gets a high
execution score but lower alignment.

If no `## Session Intent` block is present, omit the alignment fields.

**Alignment scale**:
- 0.9-1.0: Session exactly hit its declared objective and produced the
  expected artifact (or superseded it with a better outcome).
- 0.7-0.8: Session achieved its objective but the artifact was partial or
  the scope drifted slightly.
- 0.5-0.6: Session delivered something useful, but it materially diverged
  from the declared objective (pivot).
- 0.3-0.4: Session touched the intent area but produced a different or
  weakly-related outcome.
- 0.0-0.2: Session output is unrelated to the declared intent.

**Alignment verdict** (`pivot_verdict`):
- ``"on_track"``: The session hit its declared objective and produced the
  expected artifact (or clearly superseded it with a better outcome).
- ``"partial"``: The session mostly achieved the objective, but the artifact
  was partial or the scope drifted slightly.
- ``"pivot"``: The session delivered something useful, but it materially
  diverged from the declared objective.
- ``"off_target"``: The session output is unrelated or only weakly related
  to the declared intent.

**Self-assigned calibration**: If the session already self-assigned an
alignment verdict (``on_track`` / ``partial`` / ``pivot`` / ``off_target``),
use it as calibration — your score should agree in direction unless you
have strong evidence otherwise.

Return JSON: {{"score": <float>, "reason": "<1 sentence>", "alignment_score": <float or null>, "pivot_verdict": <"on_track"|"partial"|"pivot"|"off_target"|null>}}"""


def format_intent_context(intent: dict | None) -> str:
    """Render an `## Session Intent` block from a pre-session intent payload.

    Returns an empty string when ``intent`` is None or lacks the required fields.
    The string is plugged into ``JUDGE_PROMPT_TEMPLATE`` between the Routing
    Context and Session Journal blocks (before the rubric).

    Expected keys (all required for a non-empty return):
    - ``session_id`` (str): Canonical session ID.
    - ``lane`` (str): CASCADE tier/category, e.g. ``"Tier3:internal-code"``.
    - ``objective`` (str): One-sentence session goal.
    - ``expected_artifact`` (str): One-sentence concrete output.
    - ``outcome_alignment`` (str | None): Self-assigned alignment verdict
      (``"on_track"``, ``"partial"``, ``"pivot"``, ``"off_target"``, or None).

    The presence of ``outcome_alignment`` switches the block from pre-session
    intent (no self-grade yet) to post-session intent (self-assigned verdict
    included as judge calibration signal).
    """
    if not intent or not isinstance(intent, dict):
        return ""
    required = {"objective", "expected_artifact", "lane"}
    if not required.issubset(intent):
        return ""
    lines = [
        "",
        "## Session Intent",
        f"- **Objective**: {intent['objective']}",
        f"- **Expected artifact**: {intent['expected_artifact']}",
        f"- **Lane**: {intent['lane']}",
    ]
    alignment = intent.get("outcome_alignment")
    if alignment is not None:
        lines.append(f"- **Self-assigned alignment**: {alignment}")
    return "\n".join(lines) + "\n"


DEFAULT_GOALS = """\
The agent is a general-purpose autonomous AI assistant.
1. Complete assigned tasks effectively
2. Write high-quality code and documentation
3. Self-improve through lessons and patterns
4. Contribute to open-source projects"""


def format_routing_context(cascade_context: dict | None) -> str:
    """Render a `## Routing Context` block from a CASCADE selector payload.

    Returns an empty string when ``cascade_context`` is None or doesn't
    describe a legitimate lower-tier route. The string is plugged into
    JUDGE_PROMPT_TEMPLATE between Session Category and Session Journal.

    Recognised keys (all optional):
    - ``tier`` (int): CASCADE tier the selector landed on. 1/2 are
      ignored (those *are* top-priority work). 3 enables the block.
    - ``blocked_tier1_2_count`` (int): how many higher-tier tasks were
      unactionable at selection time.
    - ``selector_reason`` (str): human-readable selector rationale,
      typically the top item from cascade_intent["reasons"].

    The block intentionally only fires for Tier 3 + explicit blocked
    context, so callers without that data degrade to "no adjustment".
    """
    if not cascade_context or not isinstance(cascade_context, dict):
        return ""
    tier = cascade_context.get("tier")
    if tier != 3:
        return ""
    blocked = cascade_context.get("blocked_tier1_2_count")
    _reason_raw = cascade_context.get("selector_reason")
    reason = _reason_raw if isinstance(_reason_raw, str) else ""
    lines = [
        "",
        "## Routing Context",
        "Session legitimately routed to Tier 3 (self-improvement / strategic /",
        "cleanup / knowledge / research / content / novelty) because higher-tier",
        "work was unavailable at selection time.",
    ]
    if isinstance(blocked, int) and not isinstance(blocked, bool) and blocked > 0:
        lines.append(f"- Higher-tier tasks blocked / waiting at selection time: {blocked}")
    if reason:
        # Keep selector reasoning short; one line is enough signal for the judge.
        lines.append(f"- Selector reason: {reason[:200]}")
    return "\n".join(lines) + "\n"


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


# Models that require temperature=1.0 (Anthropic extended-thinking variants).
_EXTENDED_THINKING_PREFIXES = ("claude-3-7-",)


def _is_extended_thinking_model(model: str) -> bool:
    """Return True for models that require temperature=1.0 (extended-thinking API constraint)."""
    bare = _strip_anthropic_prefix(model)
    return any(bare.startswith(p) for p in _EXTENDED_THINKING_PREFIXES)


def _judge_backend(model: str) -> str:
    if _is_anthropic_direct_model(model):
        return "anthropic-direct"
    return "gptme-fallback"


def _build_judge_meta(*, model: str) -> JudgeMetadata:
    return {
        "backend": _judge_backend(model),
        "judge_version": JUDGE_VERSION,
    }


VALID_ALIGNMENT_VALUES = frozenset({"on_track", "partial", "pivot", "off_target"})


def _coerce_alignment_score(raw: Any) -> float | None:
    """Normalize an alignment_score value to float 0.0-1.0 or None."""
    if raw is None:
        return None
    try:
        val = float(raw)
        if not (0.0 <= val <= 1.0):
            return None
        return val
    except (ValueError, TypeError):
        return None


def _coerce_pivot_verdict(raw: Any) -> str | None:
    """Normalize a pivot_verdict/alignment label to a recognised string or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s in VALID_ALIGNMENT_VALUES else None


def normalize_judge_verdict(payload: dict[str, Any]) -> JudgeVerdict:
    """Attach stable metadata to a raw judge verdict.

    When the payload includes ``alignment_score`` and/or ``pivot_verdict``
    (Phase 3: intent contract), those fields are preserved in the output.
    """
    model = str(payload.get("model", ""))
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    base_meta = _build_judge_meta(model=model)
    verdict: JudgeVerdict = {
        "score": max(0.0, min(1.0, float(payload.get("score", 0.5)))),
        "reason": str(payload.get("reason", "")),
        "model": model,
        "alignment_score": _coerce_alignment_score(payload.get("alignment_score")),
        "pivot_verdict": _coerce_pivot_verdict(payload.get("pivot_verdict")),
        "meta": {
            "backend": str(meta.get("backend", base_meta["backend"])),
            "judge_version": str(meta.get("judge_version", base_meta["judge_version"])),
        },
    }
    return verdict


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
    result: dict[str, Any] = {"score": score, "reason": reason, "model": model}
    # Phase 3: pass through intent-contract fields when the judge returns them
    alignment_score = _coerce_alignment_score(verdict.get("alignment_score"))
    if alignment_score is not None:
        result["alignment_score"] = alignment_score
    pivot_verdict = _coerce_pivot_verdict(verdict.get("pivot_verdict"))
    if pivot_verdict is not None:
        result["pivot_verdict"] = pivot_verdict
    return result


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
    temperature: float = 0.3,
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

    # Extended-thinking models require temperature=1.0; guard against future
    # DEFAULT_JUDGE_MODEL changes that would otherwise cause an API 400.
    effective_temperature = 1.0 if _is_extended_thinking_model(model) else temperature
    if effective_temperature != temperature:
        logger.debug(
            "Extended-thinking model %s requires temperature=1.0; overriding %.2f",
            model,
            temperature,
        )

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=_strip_anthropic_prefix(model),
            max_tokens=150,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            temperature=effective_temperature,
        )
        text = getattr(response.content[0], "text", "").strip()
    except Exception as exc:
        logger.warning("LLM judge (anthropic) failed: %s", exc)
        return None

    return _parse_judge_payload(text, model)


def _judge_via_gptme(prompt: str, *, model: str) -> dict | None:
    """Call the judge via ``gptme.llm.reply`` for non-Anthropic-direct models.

    Temperature is not explicitly set here — gptme.llm.reply defaults to the
    model API default (typically 1.0 for Claude), which already provides
    sufficient variance. The caller's ``temperature`` param is applied by
    the anthropic-direct path only.
    """
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
    cascade_context: dict | None = None,
    intent: dict | None = None,
    temperature: float = 0.3,
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
        cascade_context: Optional CASCADE selector payload describing
            which tier the session legitimately routed to. When the
            payload signals a Tier 3 fallback (higher-tier work was
            blocked), the prompt instructs the judge to score primarily
            on execution quality rather than penalize for not advancing
            top-priority goals. See :func:`format_routing_context` for
            the recognised keys. Pass ``None`` (default) to keep prompt
            behaviour unchanged.
        intent: Optional pre-session intent dict (Phase 3: intent contract).
            When provided, an ``## Session Intent`` block is injected into
            the prompt and the judge is asked to return ``alignment_score``
            and ``pivot_verdict`` fields alongside ``score`` and ``reason``.
            Expected keys: ``session_id``, ``lane``, ``objective``,
            ``expected_artifact``, and optionally ``outcome_alignment``
            (self-assigned pre-closeout verdict).
        temperature: Sampling temperature for the judge model (default 0.3).
            Higher values increase score variance; 0.0 produces near-constant
            scores that undermine calibration.

    Returns:
        Dict with keys ``score`` (float), ``reason`` (str), ``model`` (str),
        and when ``intent`` is provided also ``alignment_score`` (float | None)
        and ``pivot_verdict`` (str | None). Returns ``None`` if the evaluation
        fails (missing API key, import error, non-JSON response, etc.).
    """
    # Truncate journal to ~4000 chars to keep costs low
    truncated = journal_text[:4000] if journal_text else "(empty session)"

    # str.format() only parses {placeholder} in the template itself — values
    # passed as keyword arguments are substituted verbatim without re-parsing.
    # No escaping of {/} in user content is needed or correct.
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        goals=goals,
        category=category or "unknown",
        routing_context=format_routing_context(cascade_context),
        intent_context=format_intent_context(intent),
        journal=truncated,
    )

    if _is_anthropic_direct_model(model):
        return _judge_via_anthropic_direct(
            prompt, model=model, api_key=api_key, temperature=temperature
        )
    return _judge_via_gptme(prompt, model=model)


def judge_session_with_fallback(
    journal_text: str,
    category: str | None = None,
    *,
    goals: str = DEFAULT_GOALS,
    default_model: str = DEFAULT_JUDGE_MODEL,
    fallback_models: tuple[str, ...] = (),
    api_key: str | None = None,
    cascade_context: dict | None = None,
    intent: dict | None = None,
    temperature: float = 0.3,
) -> dict | None:
    """Score a session, trying fallback models if the primary is unavailable.

    Args:
        journal_text: The session journal/summary text to evaluate.
        category: Work category (e.g. "code", "triage", "content").
        goals: Agent goals description (ordered by priority).
        default_model: Primary judge model. Tried first.
        fallback_models: Additional models to try in order when the primary fails.
        api_key: Anthropic API key (Anthropic-direct path only).
        cascade_context: Optional CASCADE selector payload. See :func:`judge_session`.
        intent: Optional pre-session intent dict. See :func:`judge_session`.
        temperature: Sampling temperature forwarded to :func:`judge_session`.

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
            cascade_context=cascade_context,
            intent=intent,
            temperature=temperature,
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
    """Persist an alignment verdict onto an existing session record.

    Also opportunistically populates ``span_aggregates`` from the session's
    trajectory when one is available. This is the natural integration point:
    the record is already being loaded and rewritten, so the extra work is
    amortized and keeps per-tool-call span data flowing into the LOO /
    analytics pipelines that key off ``SessionRecord``.
    """
    store = SessionStore(sessions_dir=sessions_dir)
    records = store.load_all()
    normalized = normalize_judge_verdict(dict(verdict))
    for record in records:
        if record.session_id != session_id:
            continue

        # Merge trajectory_ref data (harness + trajectory_path) when missing
        # from the stored record.  codex sessions in particular carry these
        # fields only in the per-session trajectory_ref.json sidecar, not
        # in the main JSONL store, so populate_span_aggregates() skips them
        # unless we merge here.
        if not record.trajectory_path or (not record.harness or record.harness == "unknown"):
            traj_ref_path = sessions_dir / f"{session_id}.trajectory_ref.json"
            if traj_ref_path.exists():
                try:
                    traj_ref = json.loads(traj_ref_path.read_text())
                    if not record.trajectory_path and traj_ref.get("trajectory_path"):
                        record.trajectory_path = traj_ref["trajectory_path"]
                    backend = traj_ref.get("backend")
                    if backend and (not record.harness or record.harness == "unknown"):
                        record.harness = backend
                except (json.JSONDecodeError, OSError):
                    pass

        record.set_alignment_grade(
            normalized["score"],
            reason=normalized["reason"],
            model=normalized["model"],
        )
        # Phase 3: persist intent-contract alignment fields via legacy bridge
        legacy_fields = getattr(record, "_legacy_fields", None)
        if isinstance(legacy_fields, dict):
            if normalized.get("alignment_score") is not None:
                legacy_fields["alignment_score"] = normalized["alignment_score"]
            if normalized.get("pivot_verdict") is not None:
                legacy_fields["pivot_verdict"] = normalized["pivot_verdict"]
        _store_judge_meta(record, normalized.get("meta"))
        # Safe to run unconditionally: returns False when trajectory_path is
        # missing / unreadable or harness is unknown, and is idempotent when
        # re-run after new trajectory data arrives.
        try:
            record.populate_span_aggregates()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "populate_span_aggregates failed for %s: %s",
                session_id,
                exc,
            )
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
    cascade_context: dict | None = None,
    intent: dict | None = None,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Judge a session and persist the verdict via SessionStore.

    If ``fallback_models`` is provided, tries them in order when ``model`` fails.
    See :func:`judge_session` for ``cascade_context`` semantics.
    """
    if fallback_models:
        verdict = judge_session_with_fallback(
            text,
            category=category,
            goals=goals,
            default_model=model,
            fallback_models=fallback_models,
            api_key=api_key,
            cascade_context=cascade_context,
            intent=intent,
            temperature=temperature,
        )
    else:
        verdict = judge_session(
            text,
            category=category,
            goals=goals,
            model=model,
            api_key=api_key,
            cascade_context=cascade_context,
            intent=intent,
            temperature=temperature,
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
