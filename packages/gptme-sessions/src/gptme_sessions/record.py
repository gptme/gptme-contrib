"""SessionRecord dataclass and model alias normalization."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Normalize model names to short canonical forms
MODEL_ALIASES: dict[str, str] = {
    # Anthropic Claude models (bare)
    "claude-opus-4-6": "opus",
    "claude-opus-4-5": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4-5": "sonnet",
    "claude-sonnet-4-20250514": "sonnet",
    "claude-haiku-4-5": "haiku",
    "claude-3-opus": "opus",
    "claude-3-sonnet": "sonnet",
    "claude-3-haiku": "haiku",
    "claude-3.5-sonnet": "sonnet",
    "claude-3.5-haiku": "haiku",
    # Anthropic provider-prefixed
    "anthropic/claude-opus-4-6": "opus",
    "anthropic/claude-opus-4-5": "opus",
    "anthropic/claude-sonnet-4-6": "sonnet",
    "anthropic/claude-sonnet-4-5": "sonnet",
    "anthropic/claude-sonnet-4-20250514": "sonnet",
    "anthropic/sonnet-4-20250514": "sonnet",
    "anthropic/claude-haiku-4-5": "haiku",
    # OpenRouter Anthropic
    "openrouter/anthropic/claude-opus-4.6": "opus",
    "openrouter/anthropic/claude-opus-4-6": "opus",
    "openrouter/anthropic/claude-opus-4.5": "opus",
    "openrouter/anthropic/claude-opus-4-5": "opus",
    "openrouter/anthropic/claude-sonnet-4-6": "sonnet",
    "openrouter/anthropic/claude-sonnet-4-5": "sonnet",
    # OpenAI models
    "openai-subscription/gpt-5.3-codex": "gpt-5.3-codex",
    "openai-subscription/gpt-5.3": "gpt-5.3",
    "openai-subscription/gpt-5.2": "gpt-5.2",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "openai/gpt-4o-mini": "gpt-4o-mini",
    "openai": "gpt-4o",  # legacy default
    # OpenRouter non-Anthropic
    "openrouter/z-ai/glm-5@z-ai": "glm-5",
    "openrouter/moonshotai/kimi-k2.5@moonshotai": "kimi-k2.5",
    "openrouter/moonshotai/kimi-k2.5": "kimi-k2.5",
    "openrouter/moonshotai/kimi-k2.5-instruct": "kimi-k2.5",
    "openrouter/minimax/minimax-m2": "minimax-m2",
    "openrouter/google/gemini-3-flash": "gemini-3-flash",
    "xai/grok-4-1-fast": "grok-4-1",
}


def normalize_model(raw: str | None) -> str | None:
    """Normalize a raw model string to its canonical short form."""
    if not raw:
        return raw
    # Exact match first.
    if raw in MODEL_ALIASES:
        return MODEL_ALIASES[raw]
    # Prefix match — require '/' separator to avoid "openai" absorbing
    # "openai-subscription/*" or similar unlisted variants.
    for prefix, canonical in MODEL_ALIASES.items():
        if raw.startswith(prefix + "/"):
            return canonical
    return raw


@dataclass
class SessionRecord:
    """Canonical per-session metadata record.

    Captures the operational context of a session: what harness ran it,
    which model, what type of work, and the outcome. Designed to be the
    single source of truth for model effectiveness analysis.
    """

    # Identity
    session_id: str = ""
    timestamp: str = ""  # ISO 8601

    # Operational context
    harness: str = "unknown"  # claude-code, gptme, codex
    model: str | None = "unknown"  # opus, sonnet, haiku, gpt-5.3-codex
    run_type: str | None = "unknown"  # autonomous, monitoring, email, review, twitter

    # Work classification
    category: str | None = None  # e.g. code, triage, content, strategic
    selector_mode: str | None = None  # e.g. scored, llm-context

    # Outcome
    outcome: str = "unknown"  # productive, noop, failed
    duration_seconds: int = 0
    token_count: int | None = None

    # Artifacts
    deliverables: list[str] = field(default_factory=list)  # commit SHAs, PR URLs
    journal_path: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        elif "T24:" in self.timestamp:
            # Fix invalid hour 24 from filenames like 240000-session.md
            self.timestamp = self.timestamp.replace("T24:00:00", "T23:59:59")
        # Normalize model names
        self.model = normalize_model(self.model)
        # Normalize run_type — reject numeric values (session numbers)
        if self.run_type and self.run_type.isdigit():
            self.run_type = "autonomous"
        # Clean run_type prefixes
        if self.run_type and self.run_type.startswith("autonomous-session"):
            self.run_type = "autonomous"

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SessionRecord:
        """Deserialize from dict, ignoring unknown fields."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def to_json(self) -> str:
        """Serialize to single-line JSON."""
        return json.dumps(self.to_dict(), default=str)
