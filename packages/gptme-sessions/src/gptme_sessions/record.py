"""SessionRecord dataclass and model alias normalization."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Harm category taxonomy (idea #191 — ESRRSim-inspired, 7×~3 subcategories).
# See knowledge/technical-designs/dual-rubric-harm-category-taxonomy.md.
HARM_CATEGORY_TAXONOMY: dict[str, str] = {
    "deception": "Agent misrepresented its actions, output, or reasoning — claimed work it didn't do",
    "evaluation_gaming": "Agent optimized for scoring metrics at the expense of actual quality",
    "reward_hacking": "Agent found a shortcut that satisfied a local goal while violating the intended outcome",
    "scope_creep": "Agent expanded its remit beyond the task boundary, causing unintended side effects",
    "tool_misuse": "Agent used a tool in a destructive or unauthorized way (rm -rf, force-push, secret leak)",
    "coordination_harm": "Agent interfered with another agent's work (duplicate PR, lock violation, overwrite)",
    "catastrophic": "Agent performed a large-scale destructive or irreversible action",
}
HARM_CATEGORY_LABELS: list[str] = list(HARM_CATEGORY_TAXONOMY.keys())

# Normalize model names to short canonical forms
MODEL_ALIASES: dict[str, str] = {
    # Anthropic Claude models (bare) — dash and dot variants
    "claude-opus-4-6": "opus",
    "claude-opus-4.6": "opus",
    "claude-opus-4-5": "opus",
    "claude-opus-4.5": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4.6": "sonnet",
    "claude-sonnet-4-5": "sonnet",
    "claude-sonnet-4.5": "sonnet",
    "claude-sonnet-4-20250514": "sonnet",
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4.5": "haiku",
    "claude-3-opus": "opus",
    "claude-3-sonnet": "sonnet",
    "claude-3-haiku": "haiku",
    "claude-3.5-sonnet": "sonnet",
    "claude-3.5-haiku": "haiku",
    # Anthropic provider-prefixed
    "anthropic/claude-opus-4-6": "opus",
    "anthropic/claude-opus-4.6": "opus",
    "anthropic/claude-opus-4-5": "opus",
    "anthropic/claude-opus-4.5": "opus",
    "anthropic/claude-sonnet-4-6": "sonnet",
    "anthropic/claude-sonnet-4.6": "sonnet",
    "anthropic/claude-sonnet-4-5": "sonnet",
    "anthropic/claude-sonnet-4.5": "sonnet",
    "anthropic/claude-sonnet-4-20250514": "sonnet",
    "anthropic/sonnet-4-20250514": "sonnet",
    "anthropic/claude-haiku-4-5": "haiku",
    "anthropic/claude-haiku-4.5": "haiku",
    # OpenRouter Anthropic
    "openrouter/anthropic/claude-opus-4.6": "opus",
    "openrouter/anthropic/claude-opus-4-6": "opus",
    "openrouter/anthropic/claude-opus-4.5": "opus",
    "openrouter/anthropic/claude-opus-4-5": "opus",
    "openrouter/anthropic/claude-sonnet-4-6": "sonnet",
    "openrouter/anthropic/claude-sonnet-4.6": "sonnet",
    "openrouter/anthropic/claude-sonnet-4-5": "sonnet",
    "openrouter/anthropic/claude-sonnet-4.5": "sonnet",
    # OpenAI models
    "openai-subscription/gpt-5.4": "gpt-5.4",
    "openai-subscription/gpt-5.3-codex": "gpt-5.3-codex",
    "openai-subscription/gpt-5.3": "gpt-5.3",
    "openai-subscription/gpt-5.2": "gpt-5.2",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "openai/gpt-4o-mini": "gpt-4o-mini",
    "openai": "gpt-4o",  # legacy default
    # OpenRouter non-Anthropic
    "openrouter/z-ai/glm-5@z-ai": "glm-5",
    "openrouter/z-ai/glm-5": "glm-5",
    "glm-5": "glm-5",
    "openrouter/moonshotai/kimi-k2.5@moonshotai": "kimi-k2.5",
    "openrouter/moonshotai/kimi-k2.5": "kimi-k2.5",
    "openrouter/moonshotai/kimi-k2.5-instruct": "kimi-k2.5",
    "openrouter/minimax/minimax-m2": "minimax-m2",
    "openrouter/minimax/minimax-m2.5": "minimax-m2.5",
    "openrouter/minimax/minimax-m2.7": "minimax-m2.7",
    "minimax-m2.5": "minimax-m2.5",
    "minimax-m2.7": "minimax-m2.7",
    "openrouter/google/gemini-3-flash": "gemini-3-flash",
    "openrouter/google/gemini-3.1-flash": "gemini-3.1-flash",
    "openrouter/google/gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-3.1-flash": "gemini-3.1-flash",
    "gemini-3.1-pro": "gemini-3.1-pro",
    "xai/grok-4-1-fast": "grok-4-1",
    # Qwen models
    "qwen3.5-coder": "qwen3.5-coder",
    "openrouter/qwen/qwen3.5-coder": "qwen3.5-coder",
}

# Regex fallback: strip known provider prefixes for models not in the alias table.
_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:openrouter/[^/]+/|anthropic/|openai-subscription/|openai/|xai/)"
)


def normalize_model(raw: str | None) -> str | None:
    """Normalize a raw model string to its canonical short form."""
    if not raw:
        return raw
    # Exact match first.
    if raw in MODEL_ALIASES:
        return MODEL_ALIASES[raw]
    # Prefix match — only for keys that already contain '/' (provider-prefixed
    # aliases).  Skip bare names like "openai" to avoid absorbing "openai/*".
    for prefix, canonical in MODEL_ALIASES.items():
        if "/" in prefix and raw.startswith(prefix + "/"):
            return canonical
    # Regex fallback: strip provider prefixes for unknown models.
    stripped = _PROVIDER_PREFIX_RE.sub("", raw)
    # Strip trailing @provider suffixes (e.g. "glm-5@z-ai" → "glm-5")
    stripped = re.sub(r"@[^@]+$", "", stripped)
    if stripped != raw:
        return stripped
    return raw


def normalize_run_type(raw: str | None) -> str | None:
    """Normalize a raw run_type string (reject numeric session numbers, clean prefixes)."""
    if not raw:
        return raw
    if raw.isdigit():
        return "autonomous"
    if raw.startswith("autonomous-session"):
        return "autonomous"
    return raw


def compute_trajectory_grade(
    grades: dict[str, float],
    weights: dict[str, float],
) -> float | None:
    """Compute a weighted-average trajectory grade from a grades vector.

    Only dimensions present in *both* ``grades`` and ``weights`` contribute.
    Missing dimensions are excluded from the denominator so their absence
    doesn't artificially depress the score.

    Returns ``None`` when there are no overlapping dimensions (can't grade).

    Example::

        >>> compute_trajectory_grade({"productivity": 0.8, "alignment": 0.7},
        ...                          {"productivity": 0.4, "alignment": 0.35, "harm": 0.25})
        0.7538461538461538
    """
    numerator = sum(grades[k] * weights[k] for k in grades if k in weights)
    denominator = sum(weights[k] for k in grades if k in weights)
    if denominator == 0.0:
        return None
    return numerator / denominator


@dataclass
class SessionRecord:
    """Canonical per-session metadata record.

    Captures the operational context of a session: what harness ran it,
    which model, what type of work, and the outcome. Designed to be the
    single source of truth for model effectiveness analysis.

    The ``model`` field stores the **raw** model string as provided
    (e.g. ``"claude-opus-4-6"``).  Use :pyattr:`model_normalized` for
    the canonical short form (e.g. ``"opus"``).  This keeps full
    provenance in storage while allowing convenient grouping in display.
    """

    # Identity
    session_id: str = ""
    timestamp: str = ""  # ISO 8601
    session_name: str | None = None  # human-readable name (e.g. "dancing-blue-fish")
    project: str | None = None  # workspace/project path

    # Operational context
    harness: str | None = "unknown"  # claude-code, gptme, codex
    model: str | None = "unknown"  # raw model string (e.g. claude-opus-4-6)
    context_tier: str | None = None  # standard, extended, large, massive
    ab_group: str | None = None  # A/B group assignment ("treatment" or "control")
    tier_version: str | None = None  # version of context tier config used
    run_type: str | None = "unknown"  # deprecated: use trigger instead
    trigger: str | None = None  # how session started: timer, dispatch, manual, spawn

    # Work classification
    category: str | None = None  # inferred from commits/files (what actually happened)
    recommended_category: str | None = None  # from Thompson sampling / CASCADE (what was intended)
    selector_mode: str | None = None  # e.g. scored, llm-context

    # Outcome
    outcome: str = "unknown"  # productive, noop, failed
    exit_code: int | None = None  # process exit code (124 = timeout)
    duration_seconds: int = 0
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None

    # Artifacts
    deliverables: list[str] = field(default_factory=list)  # commit SHAs, PR URLs
    trajectory_path: str | None = None  # path to trajectory JSONL file (for deduplication)
    journal_path: str | None = None  # path to human-written journal entry

    # Per-dimension grades for the multivariate grading rollout.
    grades: dict[str, float] = field(default_factory=dict)
    grade_reasons: dict[str, str] = field(default_factory=dict)

    # Trajectory-based grade (from signal extraction, 0.0-1.0)
    trajectory_grade: float | None = None

    # LLM judge evaluation
    llm_judge_score: float | None = None  # 0.0-1.0 goal-alignment score
    llm_judge_reason: str | None = None  # 1-sentence explanation
    llm_judge_model: str | None = None  # model used for judging (e.g. claude-haiku-4-5)

    # Optional harm category tag (idea #191).  Populated by the
    # --classify-harm-category classifier in compute-harm-signal.py.
    # One of HARM_CATEGORY_LABELS, or None when not classified.
    harm_category: str | None = None

    # Per-tool-call span aggregates (Phase 3 of span-level tracing, idea #158).
    # Dict shape mirrors SpanAggregates fields (total_spans, error_spans,
    # error_rate, dominant_tool, avg_duration_ms, max_duration_ms,
    # tool_counts, retry_depth) so downstream consumers can read it without
    # importing gptme_sessions.spans. ``None`` means not yet populated.
    span_aggregates: dict[str, Any] | None = None

    # Preserve fields written by older schema versions so load→mutate→rewrite
    # round-trips don't silently drop data (e.g. ``inferred_category``,
    # ``failure_reason``, ``recommended_confidence``, ``notes``).
    _legacy_fields: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        elif "T24:" in self.timestamp:
            # Fix invalid hour 24 from filenames like 240000-session.md
            if "T24:00:00" in self.timestamp:
                # ISO 8601 T24:00:00 = midnight; map to end of previous day
                self.timestamp = self.timestamp.replace("T24:00:00", "T23:59:59")
            else:
                # Non-midnight T24 (truly invalid) — map to T23 to at least parse
                self.timestamp = re.sub(r"T24:(\d{2}:\d{2})", r"T23:\1", self.timestamp)
        # Guard against JSON null for integer field
        if self.duration_seconds is None:
            self.duration_seconds = 0
        if self.grades is None:
            self.grades = {}
        if self.grade_reasons is None:
            self.grade_reasons = {}
        # Model stored as-is (raw) — use model_normalized for display
        # Normalize run_type — reject numeric values (session numbers) and clean prefixes
        self.run_type = normalize_run_type(self.run_type)

    def set_productivity_grade(self, score: float) -> None:
        """Store the productivity dimension alongside the legacy scalar field."""
        self.grades["productivity"] = score
        self.trajectory_grade = score

    def set_alignment_grade(
        self,
        score: float,
        *,
        reason: str | None = None,
        model: str | None = None,
    ) -> None:
        """Store the alignment dimension alongside legacy judge fields."""
        self.llm_judge_score = score
        self.grades["alignment"] = score
        self.llm_judge_reason = reason
        if reason is not None:
            self.grade_reasons["alignment"] = reason
        else:
            self.grade_reasons.pop("alignment", None)
        if model is not None:
            self.llm_judge_model = model

    def sync_grade_fields(self) -> bool:
        """Backfill multivariate grade fields from legacy scalar fields.

        Returns ``True`` when any missing ``grades``/``grade_reasons`` entry
        was added, otherwise ``False``.
        """
        changed = False
        if self.trajectory_grade is not None and "productivity" not in self.grades:
            self.grades["productivity"] = self.trajectory_grade
            changed = True
        if self.llm_judge_score is not None and "alignment" not in self.grades:
            self.grades["alignment"] = self.llm_judge_score
            changed = True
        if (
            self.llm_judge_reason is not None
            and "alignment" in self.grades
            and "alignment" not in self.grade_reasons
        ):
            self.grade_reasons["alignment"] = self.llm_judge_reason
            changed = True
        return changed

    def apply_weighted_grade(self, weights: dict[str, float]) -> float | None:
        """Recompute trajectory_grade as a weighted average of populated grade dims.

        Only dims present in both ``self.grades`` and ``weights`` contribute.
        Missing dims are excluded from the denominator (no penalty for absent signals).
        Updates ``self.trajectory_grade`` in place and returns the new value.
        Returns ``None`` when no overlapping dims are found.
        """
        result = compute_trajectory_grade(self.grades, weights)
        if result is not None:
            self.trajectory_grade = result
        return result

    def populate_span_aggregates(self, harness_hint: str | None = None) -> bool:
        """Extract tool spans from ``trajectory_path`` and store aggregates.

        Chooses the extractor based on ``harness_hint`` (or ``self.harness``
        when not given): ``"claude-code"`` → CC JSONL, ``"gptme"`` → gptme
        JSONL. Stores ``SpanAggregates.from_spans(...)`` serialized as a dict
        on ``self.span_aggregates`` (including the computed ``error_rate``).

        Returns ``True`` when aggregates were populated, ``False`` when the
        trajectory path is missing, unreadable, or the harness is unknown.
        Idempotent: safe to re-run when new trajectory data arrives.
        """
        # Lazy import avoids a top-level cycle and keeps record.py independent
        # of the spans module for users who never call this helper.
        from gptme_sessions.spans import (
            SpanAggregates,
            extract_spans_from_cc_jsonl,
            extract_spans_from_gptme_jsonl,
        )

        if not self.trajectory_path:
            return False
        traj = Path(self.trajectory_path)
        if not traj.exists():
            return False

        harness = harness_hint or self.harness
        if harness == "claude-code":
            spans = extract_spans_from_cc_jsonl(traj, session_id=self.session_id)
        elif harness == "gptme":
            spans = extract_spans_from_gptme_jsonl(traj, session_id=self.session_id)
        else:
            return False

        agg = SpanAggregates.from_spans(spans)
        self.span_aggregates = {
            "total_spans": agg.total_spans,
            "error_spans": agg.error_spans,
            "error_rate": agg.error_rate,
            "dominant_tool": agg.dominant_tool,
            "avg_duration_ms": agg.avg_duration_ms,
            "max_duration_ms": agg.max_duration_ms,
            "tool_counts": agg.tool_counts,
            "retry_depth": agg.retry_depth,
        }
        return True

    @property
    def model_normalized(self) -> str | None:
        """Canonical short form of the model (e.g. ``"opus"``)."""
        return normalize_model(self.model)

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict.

        Legacy/unknown fields captured in ``_legacy_fields`` during
        ``from_dict`` are re-emitted alongside dataclass fields so that
        load→rewrite cycles preserve them.
        """
        d = asdict(self)
        legacy = d.pop("_legacy_fields", {}) or {}
        for k, v in legacy.items():
            # Known fields always win — we only add legacy keys that aren't
            # already present as first-class fields.
            d.setdefault(k, v)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> SessionRecord:
        """Deserialize from dict, preserving unknown fields for round-trip.

        Backward compat: old records stored the trajectory path (JSONL file or
        session directory) in ``journal_path`` before ``trajectory_path`` was
        added.  Migrate those by moving the value to ``trajectory_path`` when it
        does **not** end with ``.md``.

        Human-written journal entries are always Markdown files (``.md``).
        Any ``journal_path`` value that isn't a ``.md`` file is therefore a
        trajectory path stored by the old ``sync`` command and should be
        migrated — whether it's a ``.jsonl`` file or a bare session directory
        (the latter occurring for gptme sessions that lack ``conversation.jsonl``).

        Any field not defined on the dataclass (e.g. legacy columns from older
        schema versions) is captured into ``_legacy_fields`` so that subsequent
        ``to_dict`` / ``rewrite`` calls preserve it.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        legacy = {k: v for k, v in data.items() if k not in known_fields and not k.startswith("_")}
        # Migrate legacy records: journal_path that is not a .md file is
        # actually a trajectory path (JSONL or session directory) set by the
        # old sync command before trajectory_path was introduced.
        if (
            "trajectory_path" not in filtered
            and isinstance(filtered.get("journal_path"), str)
            and not filtered["journal_path"].endswith(".md")
        ):
            filtered["trajectory_path"] = filtered.pop("journal_path")
        if legacy:
            filtered["_legacy_fields"] = legacy
        return cls(**filtered)

    def to_json(self) -> str:
        """Serialize to single-line JSON."""
        return json.dumps(self.to_dict(), default=str)
