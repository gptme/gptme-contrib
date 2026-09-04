"""SessionRecord dataclass and model alias normalization."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .deliverables import looks_like_sha

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

# Valid values for the attempt_kind field (eval attempt classification).
#
#   repetition  — a normal measurable sample: whatever the session produced is
#                 quality signal and may feed grades/bandits.
#   infra_retry — the attempt was replaced/lost to capacity or harness failure
#                 (rate limit, auth, dead backend, zero-token exit).  It carries
#                 no quality signal and must be excluded from quality analytics.
#   unknown     — classification was attempted but could not be determined.
#
# A record with ``attempt_kind is None`` predates the field; it is *not* the
# same as ``"unknown"`` and consumers should fall back to their own heuristic.
ATTEMPT_KINDS: frozenset[str] = frozenset(["repetition", "infra_retry", "unknown"])

# Valid values for the dropout_depth field.
DROPOUT_DEPTH_VALUES: frozenset[str] = frozenset(["shallow", "deep"])

# How a child session was spawned by its parent.  These are NOT
# interchangeable in analysis: an ``agent-tool`` subagent shares the parent's
# context window and token budget, while every other kind is a separate process
# with its own.  ``None`` means the session has no recorded parent (a top-level
# run, or a spawner that predates the field).
DISPATCH_KINDS: frozenset[str] = frozenset(
    [
        "agent-tool",  # in-session Claude Code Agent tool subagent
        "worker",  # spawn-workers.sh child
        "gptodo-spawn",  # gptodo spawn / delegate child
        "pm-dispatch",  # project-monitoring runloops dispatch
        "fanout",  # autonomous-fanout.sh child
    ]
)

# Scalar fields the ``annotate`` CLI can explicitly override. Persisting this
# provenance lets later source extraction distinguish operator decisions from
# values it owns and may refresh.
ANNOTATABLE_FIELDS: frozenset[str] = frozenset(
    [
        "model",
        "harness",
        "run_type",
        "category",
        "outcome",
        "duration_seconds",
        "journal_path",
        "selector_mode",
        "trigger",
        "token_count",
        "recommended_category",
    ]
)


def trajectory_revision_for(path: Path) -> str | None:
    """Return a cheap revision for append-only trajectory change detection.

    Encodes device, inode, size, and mtime so an append or rewrite is visible
    without hashing file contents. ``None`` if ``path`` cannot be stat'd.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


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


_TRAILING_PAREN_SHA_RE = re.compile(r"\(([0-9a-f]{7,40})\)\s*$", re.IGNORECASE)
_LEADING_SHA_RE = re.compile(r"^([0-9a-f]{7,40})(?![0-9a-f])", re.IGNORECASE)
_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE)
_PR_NUMBER_RE = re.compile(r"PR\s+#(\d+)", re.IGNORECASE)


def _commit_identity(value: object, kind: str | None = None) -> str | None:
    """Extract a SHA identity from a commit deliverable value.

    Producers encode identity in different slots of the same string:

    - Grok ``git_commit``: ``sha message``. The leading SHA is identity; a
      parenthetical hex token in the message is not.
    - Trajectory ``commit`` / ``merge_commit``: ``message (sha)``. The trailing
      parenthetical SHA is identity; a leading hex token in the message is not.
    - Caller / unknown: a bare SHA, else the producer-encoded slot when
      present (trailing paren, then leading SHA).
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if looks_like_sha(stripped):
        return stripped.lower()
    leading = _LEADING_SHA_RE.match(stripped)
    trailing = _TRAILING_PAREN_SHA_RE.search(stripped)
    # Grok writes ``sha message``; prefer the leading token even when the
    # commit message itself ends in ``(hex)``.
    if kind == "git_commit":
        if leading:
            return leading.group(1).lower()
        if trailing:
            return trailing.group(1).lower()
        return None
    # Trajectory and merge-commit values put the real SHA in trailing parens.
    # A message that happens to start with a hex token must not win.
    if trailing:
        return trailing.group(1).lower()
    if leading:
        return leading.group(1).lower()
    return None


def _remember_commit_id(commit_ids: list[str], commit_id: str) -> None:
    """Record a SHA, merging short/full forms of the same hash."""
    for known_id in commit_ids:
        if commit_id.startswith(known_id) or known_id.startswith(commit_id):
            return
    commit_ids.append(commit_id)


def _pr_identity(value: object) -> str:
    """Stable identity for a pull-request deliverable."""
    if not isinstance(value, str):
        return str(value)
    stripped = value.strip()
    url = _PR_URL_RE.search(stripped)
    if url:
        return f"{url.group(1).lower()}/{url.group(2).lower()}#{url.group(3)}"
    number = _PR_NUMBER_RE.search(stripped)
    if number:
        return f"#{number.group(1)}"
    return stripped.lower()


def _pr_group_key(identity: str, aliasable_prs: dict[str, str]) -> str:
    """Canonical grouping key for a PR identity.

    An unqualified ``#N`` aliases to the sole qualified ``owner/repo#N`` with
    ``gh pr merge`` evidence. Qualified identities always remain distinct, so
    same-numbered PRs in different repositories cannot collapse.
    """
    return aliasable_prs.get(identity, identity)


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
    timestamp: str = ""  # ISO 8601 — record-creation time
    start_time: str | None = None  # ISO 8601 — session start (populated by post_session)
    end_time: str | None = None  # ISO 8601 — session end (populated by post_session)
    session_name: str | None = None  # human-readable name (e.g. "dancing-blue-fish")
    project: str | None = None  # workspace/project path

    # Operational context
    harness: str | None = "unknown"  # claude-code, gptme, codex, pi
    model: str | None = "unknown"  # raw model string (e.g. claude-opus-4-6)
    context_tier: str | None = None  # standard, extended, large, massive
    ab_group: str | None = None  # A/B group assignment ("treatment" or "control")
    tier_version: str | None = None  # version of context tier config used
    run_type: str | None = "unknown"  # deprecated: use trigger instead
    trigger: str | None = None  # how session started: timer, dispatch, manual, spawn

    # Out-of-process dispatch lineage.  ``parent_session_id`` is the session_id
    # of the session that spawned this one; ``dispatch_kind`` names the spawn
    # mechanism (see DISPATCH_KINDS).  Together they give product roll-ups a
    # directed join instead of timestamp-proximity guessing.
    parent_session_id: str | None = None
    dispatch_kind: str | None = None
    # Dispatcher-run id for dispatchers that are not recorded sessions themselves
    # (e.g. project-monitoring slots: PM_DISPATCH_ID = the slot unit name).
    # Complements ``parent_session_id``; use for dispatcher-run→child joins when
    # the dispatcher has no session_id of its own.
    dispatch_id: str | None = None

    # Work classification
    category: str | None = None  # inferred from commits/files (what actually happened)
    recommended_category: str | None = None  # from Thompson sampling / CASCADE (what was intended)
    selector_mode: str | None = None  # e.g. scored, llm-context
    cascade_intent: dict[str, Any] | None = None  # structured CASCADE reasons + constraints dict

    # Outcome
    outcome: str = "unknown"  # productive, noop, violated_policy, failed
    exit_code: int | None = None  # process exit code (124 = timeout)
    failure_reason: str | None = None  # coarse harness failure class (see failure_capture.py)
    error: str | None = None  # stderr tail or trajectory error snippet when harness failed
    # Eval attempt classification, written once by the session runner which
    # already computes it (see ``ATTEMPT_KINDS``).  ``None`` on every record
    # written before this field existed, so consumers must treat ``None`` as
    # "unrecorded" and fall back to their own heuristic rather than assuming
    # ``repetition``.
    attempt_kind: str | None = None
    duration_seconds: int = 0
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    sys_prompt_tokens: int | None = None
    context_peak_tokens: int | None = None
    context_window: int | None = None

    # Byte-level context metrics (model-independent; #738)
    sys_prompt_bytes: int | None = None  # system messages before first user msg
    first_turn_bytes: int | None = None  # all messages before first assistant msg
    context_peak_bytes: int | None = None  # max per-turn context bytes before assistant
    session_total_bytes: int | None = None  # total bytes of all message content

    # Artifacts
    deliverables: list[str] = field(default_factory=list)  # commit SHAs, PR URLs
    deliverable_details: list[dict[str, Any]] = field(default_factory=list)
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

    # Regex-based LLM "smell" score (0.0-1.0, voice-quality / blandness signal).
    # Computed from the journal text at post_session time by gptme_sessions.smell.
    # Higher = more machine-generated tells (hedging, vocab tics, canned voice).
    # ``None`` when no journal_path was available to scan.
    smell_score: float | None = None

    # Why the recorded outcome differs from what the primary signal said.
    # ``None`` means no flip happened — the signal was taken at face value.
    # Set by post_session() at each promotion site so an upgraded outcome
    # (e.g. noop -> productive on caller deliverables) stays auditable.
    outcome_flip_reason: str | None = None

    # Optional harm category tag (idea #191).  Populated by the
    # --classify-harm-category classifier in compute-harm-signal.py.
    # One of HARM_CATEGORY_LABELS, or None when not classified.
    harm_category: str | None = None

    # Random dropout sampling (ErikBjare/bob#793)
    # Flag set by the autonomous-run.sh reconcile pass when the session is
    # drawn for deeper post-hoc review. Dropout sessions get a richer secondary
    # analysis beyond the standard LLM judge run.
    dropout_selected: bool | None = None
    dropout_reason: str | None = None  # "random_sampling" for selected sessions; None otherwise
    dropout_depth: str | None = None  # "shallow" (standard judge) or "deep" (extra analysis)

    # Per-tool-call span aggregates (Phase 3 of span-level tracing, idea #158).
    # Dict shape mirrors SpanAggregates fields (total_spans, error_spans,
    # error_rate, dominant_tool, avg_duration_ms, max_duration_ms,
    # tool_counts, retry_depth) so downstream consumers can read it without
    # importing gptme_sessions.spans. ``None`` means not yet populated.
    span_aggregates: dict[str, Any] | None = None

    # Derived step-type labels, populated from span_aggregates immediately after
    # populate_span_aggregates() runs. Each label is a short string describing a
    # characteristic of how this session used tools (e.g. "bash_dominant",
    # "multi_commit", "deep_session"). Used by scripts/loo-step-analysis.py for
    # within-session credit assignment (LOO over step-type mixes).
    # ``None`` means not yet computed (pre-dates this field or trajectory absent).
    step_types: list[str] | None = None

    # Whether the tool-output summarizer fired at least once during this session.
    # Detected by scanning the trajectory for the summarization marker text.
    # True = summarizer ran; False = summarizer enabled but no eviction happened;
    # None = trajectory unavailable or pre-dates this field.
    summarizer_fired: bool | None = None

    # Per-step phase timings (gptme sessions only; populated from metadata.timings
    # written by gptme/gptme#3436). ``None`` for sessions before that PR or for
    # non-gptme harnesses. All values in milliseconds (wall-clock).
    ttft_ms_avg: float | None = None  # mean time-to-first-token across timed turns
    ttft_ms_p50: float | None = None  # median TTFT (more robust to outliers)
    gen_ms_total: float | None = None  # total generation (streaming) time
    tool_ms_total: float | None = None  # total tool-execution wall time

    # Harness supply: lessons injected into this session by the match-lessons
    # hook (name, file, match_type, injection_point, timestamp). Ingested by
    # post_session() from the hook's per-session events file. Empty for
    # harnesses that don't run the hook.
    lesson_events: list[dict[str, Any]] = field(default_factory=list)

    # Additive route metadata. Keep new public fields at the dataclass tail so
    # existing positional SessionRecord constructors retain their old mapping.
    provider: str | None = None  # raw inference provider (e.g. openai-codex, xai)
    stop_reason: str | None = None  # harness-native final assistant stop reason
    # Harness-reported USD-equivalent cost. For subscription/OAuth providers
    # this may be nominal API-equivalent cost, not incremental billed spend.
    cost_usd: float | None = None

    # Cheap source revision captured after a successful trajectory extraction
    # (``dev:ino:size:mtime_ns`` from :func:`trajectory_revision_for`). Pi
    # sessions are append-only and resumable, so sync uses this to refresh
    # cumulative signals when the native JSONL grows without reparsing
    # unchanged historical sessions on every run. Other harnesses persist it
    # too so later syncs can skip unchanged files and coverage can see the
    # field.
    trajectory_revision: str | None = None
    trajectory_extract_version: int | None = None

    # Fields explicitly overridden through ``gptme-sessions annotate``. Source
    # refreshes must not silently replace these operator decisions, including
    # an override that happens to equal the source value at annotation time.
    annotated_fields: list[str] = field(default_factory=list)

    # Preserve fields written by older schema versions so load→mutate→rewrite
    # round-trips don't silently drop data (e.g. ``inferred_category``,
    # ``recommended_confidence``, ``notes``).
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
        if self.deliverables is None:
            self.deliverables = []
        if self.deliverable_details is None:
            self.deliverable_details = []
        if self.lesson_events is None:
            self.lesson_events = []
        if not isinstance(self.annotated_fields, list):
            self.annotated_fields = []
        else:
            # Preserve first-write order for readable JSON while preventing
            # corrupt payloads from turning membership checks into substring
            # checks (a bare JSON string) or retaining non-field values.
            self.annotated_fields = list(
                dict.fromkeys(
                    value
                    for value in self.annotated_fields
                    if isinstance(value, str) and value in ANNOTATABLE_FIELDS
                )
            )
        if self.grades is None:
            self.grades = {}
        if self.grade_reasons is None:
            self.grade_reasons = {}
        # Model stored as-is (raw) — use model_normalized for display
        # Normalize run_type — reject numeric values (session numbers) and clean prefixes
        self.run_type = normalize_run_type(self.run_type)
        # Discard unrecognized harm_category values (e.g. from corrupt JSONL rows)
        # so downstream --by-category consumers never see unexpected keys.
        if self.harm_category is not None and self.harm_category not in HARM_CATEGORY_LABELS:
            self.harm_category = None
        # Discard unrecognized dropout_depth values so typos don't silently reach consumers.
        if self.dropout_depth is not None and self.dropout_depth not in DROPOUT_DEPTH_VALUES:
            self.dropout_depth = None
        # Discard unrecognized attempt_kind values so hand-edited JSONL or a buggy writer
        # cannot persist an invalid value that consumers would silently misclassify.
        if self.attempt_kind is not None and self.attempt_kind not in ATTEMPT_KINDS:
            self.attempt_kind = None
        # Discard unrecognized dispatch_kind values for the same reason.
        if self.dispatch_kind is not None and self.dispatch_kind not in DISPATCH_KINDS:
            self.dispatch_kind = None
        # A session cannot be its own parent.  A spawner that leaks its own id
        # into the child environment would otherwise create a self-loop that
        # silently breaks every lineage walk.
        if self.parent_session_id is not None and (
            not self.parent_session_id.strip() or self.parent_session_id == self.session_id
        ):
            self.parent_session_id = None
        # Cross-field consistency: when explicitly not selected (False), the detail fields
        # have no meaning — clear them to prevent downstream consumers from reading an
        # inconsistent combination (e.g. dropout_selected=False, dropout_depth="deep").
        if self.dropout_selected is False:
            self.dropout_reason = None
            self.dropout_depth = None

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
        JSONL, ``"codex"`` → codex rollout JSONL. Stores
        ``SpanAggregates.from_spans(...)`` serialized as a dict
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
            extract_spans_from_codex_jsonl,
            extract_spans_from_gptme_jsonl,
        )

        if not self.trajectory_path:
            return False
        traj = Path(self.trajectory_path)
        if not traj.exists():
            return False

        harness = harness_hint or self.harness
        # Fallback: detect harness from trajectory path when harness is
        # "unknown" (common for codex sessions where the trajectory_ref
        # backend field doesn't flow into SessionRecord.harness).
        if harness in ("unknown", None) and "/.codex/" in str(traj):
            harness = "codex"
        if harness == "claude-code":
            spans = extract_spans_from_cc_jsonl(traj, session_id=self.session_id)
        elif harness == "gptme":
            spans = extract_spans_from_gptme_jsonl(traj, session_id=self.session_id)
        elif harness == "codex":
            spans = extract_spans_from_codex_jsonl(traj, session_id=self.session_id)
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
        self.populate_step_types()
        return True

    def populate_step_types(self) -> None:
        """Derive step-type labels from span_aggregates and deliverable_details.

        Step types are binary attributes that characterize how a session used
        its tool budget. They enable LOO-style attribution (see
        scripts/loo-step-analysis.py) without requiring trajectory re-parsing.

        Idempotent: re-running on an already-populated record updates in place.
        """
        sa = self.span_aggregates or {}
        raw_counts: dict[str, int] = sa.get("tool_counts") or {}
        # 0 is a real observation (no completed tool spans). Do not coerce it
        # to 1 — that would stamp shallow_session on absent evidence.
        total_spans = sa.get("total_spans") or 0
        error_spans = sa.get("error_spans") or 0
        retry_depth = sa.get("retry_depth") or 0

        # Normalize harness-specific tool names onto CC-canonical labels.
        # Keys are lowercased; lookup uses tool.lower().
        # Task (CC) is the subagent tool → Agent, not TaskOp. TodoWrite is TaskOp.
        _aliases = {
            "shell": "Bash",
            "ipython": "Bash",
            "exec_command": "Bash",
            "write_stdin": "Bash",
            "exec": "Bash",
            "save": "Write",
            "write": "Write",
            "patch": "Edit",
            "append": "Edit",
            "edit": "Edit",
            "apply_patch": "Edit",
            "read": "Read",
            "todo": "TaskOp",
            "complete": "TaskOp",
            "todowrite": "TaskOp",
            "agent": "Agent",
            "subagent": "Agent",
            "task": "Agent",
        }
        tc: dict[str, int] = {}
        for tool, count in raw_counts.items():
            normalized = _aliases.get(tool.lower(), tool)
            tc[normalized] = tc.get(normalized, 0) + count

        bash_count = tc.get("Bash", 0)
        read_count = tc.get("Read", 0)
        edit_count = tc.get("Edit", 0) + tc.get("Write", 0)
        agent_count = tc.get("Agent", 0)
        task_op_count = tc.get("TaskOp", 0)

        if total_spans > 0:
            bash_ratio = bash_count / total_spans
            read_ratio = read_count / total_spans
            edit_ratio = edit_count / total_spans
            error_rate = error_spans / total_spans
        else:
            bash_ratio = read_ratio = edit_ratio = error_rate = 0.0
        tool_diversity = len([v for v in tc.values() if v > 0])

        step_types: list[str] = []

        # Tool-use patterns
        if bash_ratio > 0.60:
            step_types.append("bash_dominant")
        if read_ratio > 0.20:
            step_types.append("read_heavy")
        if edit_ratio > 0.12:
            step_types.append("edit_focused")
        if agent_count > 0:
            step_types.append("agent_spawner")
        if task_op_count > 2:
            step_types.append("task_op_heavy")

        # Session depth / diversity
        if tool_diversity >= 5:
            step_types.append("high_tool_diversity")
        if total_spans >= 40:
            step_types.append("deep_session")
        elif 0 < total_spans <= 10:
            step_types.append("shallow_session")

        # Quality / reliability
        if error_spans == 0 and total_spans >= 5:
            step_types.append("error_free")
        if retry_depth >= 2:
            step_types.append("retry_heavy")
        if error_rate > 0.10:
            step_types.append("error_prone")

        # Delivery patterns (from deliverable_details). Commit details can be
        # duplicated across trajectory and caller producers. Prefer unique SHA
        # identities; identical identity-less values also count once. A PR is
        # metadata only when its evidence ties it to one of those commits.
        dd = self.deliverable_details or []
        commit_ids: list[str] = []
        anonymous_commit_values: set[str] = set()
        for detail in dd:
            if detail.get("kind") not in {"commit", "git_commit", "merge_commit"}:
                continue
            value = detail.get("value")
            commit_id = _commit_identity(value, kind=detail.get("kind"))
            if commit_id is None:
                anonymous_commit_values.add(str(value))
                continue
            _remember_commit_id(commit_ids, commit_id)

        # merge_commits with gh_pr_merge evidence: each one can "cover" exactly
        # one gh_pr_merge-evidenced PR, preventing double-counting. A commit
        # explicitly sha-paired to a PR (below) is removed from this pool
        # first, so it can't *also* pay for a different, unrelated PR.
        gh_pr_merge_commit_ids: list[str] = []
        for detail in dd:
            evidence = detail.get("evidence")
            if (
                detail.get("kind") != "merge_commit"
                or not isinstance(evidence, dict)
                or evidence.get("action") != "gh_pr_merge"
            ):
                continue
            commit_id = _commit_identity(detail.get("value"), kind="merge_commit")
            if commit_id is not None:
                _remember_commit_id(gh_pr_merge_commit_ids, commit_id)

        # An unqualified "#N" with gh_pr_merge evidence may alias to exactly
        # one qualified PR with that number. Never alias qualified identities
        # to each other: same-numbered PRs can belong to different repositories.
        merge_numbers: set[str] = set()
        qualified_prs: dict[str, set[str]] = {}
        for detail in dd:
            if detail.get("kind") != "pull_request":
                continue
            identity = _pr_identity(detail.get("value"))
            if "#" not in identity:
                continue
            number = identity.rsplit("#", 1)[-1]
            if identity.startswith("#"):
                evidence = detail.get("evidence")
                if isinstance(evidence, dict) and evidence.get("action") == "gh_pr_merge":
                    merge_numbers.add(number)
            else:
                qualified_prs.setdefault(number, set()).add(identity)
        aliasable_prs = {
            f"#{number}": next(iter(qualified_prs[number]))
            for number in merge_numbers
            if len(qualified_prs.get(number, ())) == 1
        }

        pr_groups: dict[str, dict[str, bool]] = {}
        for detail in dd:
            if detail.get("kind") != "pull_request":
                continue
            evidence = detail.get("evidence")
            action = evidence.get("action") if isinstance(evidence, dict) else None
            evidence_sha = evidence.get("commit_sha") if isinstance(evidence, dict) else None
            key = _pr_group_key(_pr_identity(detail.get("value")), aliasable_prs)
            group = pr_groups.setdefault(key, {"sha_paired": False, "gh_pr_merge": False})
            if action == "gh_pr_merge":
                group["gh_pr_merge"] = True
            if not group["sha_paired"] and isinstance(evidence_sha, str):
                normalized_sha = _commit_identity(evidence_sha)
                if normalized_sha is not None and any(
                    normalized_sha.startswith(commit_id) or commit_id.startswith(normalized_sha)
                    for commit_id in commit_ids
                ):
                    group["sha_paired"] = True
                    for i, gh_cid in enumerate(gh_pr_merge_commit_ids):
                        if normalized_sha.startswith(gh_cid) or gh_cid.startswith(normalized_sha):
                            del gh_pr_merge_commit_ids[i]
                            break

        pr_count = 0
        gh_pr_merge_commit_budget = len(gh_pr_merge_commit_ids)
        for group in pr_groups.values():
            if group["sha_paired"]:
                continue
            if group["gh_pr_merge"] and gh_pr_merge_commit_budget > 0:
                gh_pr_merge_commit_budget -= 1
                continue
            pr_count += 1

        commit_count = len(commit_ids) + len(anonymous_commit_values) + pr_count
        file_count = sum(1 for d in dd if d.get("kind") == "file")

        # Partition on commit cardinality: 0 / 1 / 2+. Two-commit sessions
        # (commit + fixup) are multi, not a silent hole — the analysis script
        # can still subset to 3+ if it wants a stricter "iterative" cut.
        if commit_count == 0:
            step_types.append("no_commit")
        elif commit_count == 1:
            step_types.append("single_commit")
        elif commit_count >= 2:
            step_types.append("multi_commit")

        if commit_count > 0 and file_count > 0:
            step_types.append("mixed_deliverables")

        self.step_types = step_types

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
        # Migrate legacy records: dropout_selection (bool, added in #963) was
        # replaced by dropout_selected/dropout_reason/dropout_depth in #965.
        # Forward the flag so records written between the two deploys aren't silently lost.
        if "dropout_selection" in legacy and "dropout_selected" not in filtered:
            if legacy["dropout_selection"]:
                filtered["dropout_selected"] = True
                filtered["dropout_reason"] = "random_sampling"
            del legacy["dropout_selection"]
        if legacy:
            filtered["_legacy_fields"] = legacy
        return cls(**filtered)

    def to_json(self) -> str:
        """Serialize to single-line JSON."""
        return json.dumps(self.to_dict(), default=str)
