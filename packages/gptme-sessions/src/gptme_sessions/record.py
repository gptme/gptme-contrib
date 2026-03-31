"""SessionRecord dataclass and model alias normalization."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

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

    # Artifacts
    deliverables: list[str] = field(default_factory=list)  # commit SHAs, PR URLs
    trajectory_path: str | None = None  # path to trajectory JSONL file (for deduplication)
    journal_path: str | None = None  # path to human-written journal entry

    # Trajectory-based grade (from signal extraction, 0.0-1.0)
    trajectory_grade: float | None = None

    # LLM judge evaluation
    llm_judge_score: float | None = None  # 0.0-1.0 goal-alignment score
    llm_judge_reason: str | None = None  # 1-sentence explanation
    llm_judge_model: str | None = None  # model used for judging (e.g. claude-haiku-4-5)

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        elif "T24:" in self.timestamp:
            # Fix invalid hour 24 from filenames like 240000-session.md
            self.timestamp = self.timestamp.replace("T24:00:00", "T23:59:59")
        # Guard against JSON null for integer field
        if self.duration_seconds is None:
            self.duration_seconds = 0
        # Model stored as-is (raw) — use model_normalized for display
        # Normalize run_type — reject numeric values (session numbers) and clean prefixes
        self.run_type = normalize_run_type(self.run_type)

    @property
    def model_normalized(self) -> str | None:
        """Canonical short form of the model (e.g. ``"opus"``)."""
        return normalize_model(self.model)

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SessionRecord:
        """Deserialize from dict, ignoring unknown fields.

        Backward compat: old records stored the trajectory path (JSONL file or
        session directory) in ``journal_path`` before ``trajectory_path`` was
        added.  Migrate those by moving the value to ``trajectory_path`` when it
        does **not** end with ``.md``.

        Human-written journal entries are always Markdown files (``.md``).
        Any ``journal_path`` value that isn't a ``.md`` file is therefore a
        trajectory path stored by the old ``sync`` command and should be
        migrated — whether it's a ``.jsonl`` file or a bare session directory
        (the latter occurring for gptme sessions that lack ``conversation.jsonl``).
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # Migrate legacy records: journal_path that is not a .md file is
        # actually a trajectory path (JSONL or session directory) set by the
        # old sync command before trajectory_path was introduced.
        if (
            "trajectory_path" not in filtered
            and isinstance(filtered.get("journal_path"), str)
            and not filtered["journal_path"].endswith(".md")
        ):
            filtered["trajectory_path"] = filtered.pop("journal_path")
        return cls(**filtered)

    def to_json(self) -> str:
        """Serialize to single-line JSON."""
        return json.dumps(self.to_dict(), default=str)
