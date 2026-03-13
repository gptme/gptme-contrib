"""Session classification — categorize agent work sessions.

Provides configurable session classification using a hybrid approach:
- **LLM classifier** (primary): Uses a cheap LLM call to categorize sessions
- **Keyword classifier** (fallback): Section-aware weighted keyword matching

Categories are configurable — agents can define their own category sets.
Default categories cover common agent work patterns.

Integration points:
- ``gptme-sessions classify``: classify sessions from journal entries
- ``post_session()``: can auto-classify via ``classify=True``
- ``gptme-sessions judge``: can include classification alongside scoring
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Default categories (generic — agents can override)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Category:
    """A session work category with description and keyword signals."""

    name: str
    description: str
    title_keywords: list[str] = field(default_factory=list)
    outcome_keywords: list[str] = field(default_factory=list)
    execution_keywords: list[str] = field(default_factory=list)
    deliverable_keywords: list[str] = field(default_factory=list)


# Default category definitions — suitable for most agent workspaces
DEFAULT_CATEGORIES: list[Category] = [
    Category(
        name="code",
        description="Wrote code, created scripts, implemented features, bug fixes",
        title_keywords=[
            "implement",
            "refactor",
            "tool",
            "script",
            "parser",
            "cli",
            "code:",
            "code —",
            "feat(",
            "fix(",
            "bug fix",
            "feature",
            "selector",
            "optimizer",
            "migration",
            "rewrite",
            "fixes",
            "ux fix",
            "nav fix",
            "tests",
        ],
        outcome_keywords=[
            "new tool",
            "tests passing",
            "opened pr",
            "created pr",
            "submitted pr",
            "feat",
            "fix",
            "pr merged",
            "tests all pass",
            "pr submitted",
            "pr created",
            "new feature",
            "pr updated",
            "tests pass",
        ],
        execution_keywords=[
            "created branch",
            "cargo check",
            "pytest",
            "tests passing",
            "git push",
            "npm test",
            "make test",
            "ruff",
            "mypy",
            "type check",
            "linting",
            "test suite",
            "pr ",
            "new tests",
        ],
        deliverable_keywords=[
            "opened pr",
            "created pr",
            "submitted pr",
            "pull request",
            "fix(",
            "feat(",
            "refactor(",
            "pr #",
            "pr gptme",
            "pr updated",
            "pr gptme/",
            "tests passing",
            "tests all pass",
        ],
    ),
    Category(
        name="infrastructure",
        description="Built tooling, improved automation, CI/CD, system work",
        title_keywords=[
            "infrastructure:",
            "infra —",
            "health check",
            "systemd",
            "service setup",
            "ci fix",
            "build fix",
            "deploy",
            "monitoring",
            "pipeline",
            "automation",
            "config",
            "service —",
            "ops:",
            "devops",
        ],
        outcome_keywords=[
            "service deployed",
            "pipeline fixed",
            "ci green",
            "monitoring active",
            "automation working",
            "health check passing",
            "service running",
        ],
        execution_keywords=[
            "health check",
            "worktree cleanup",
            "systemd",
            "deployed",
            "ci fix",
            "build fix",
            "docker",
            "service restart",
            "cron",
            "timer",
        ],
        deliverable_keywords=[
            "service deployed",
            "ci fixed",
            "pipeline",
            "automation",
            "monitoring",
            "infrastructure",
        ],
    ),
    Category(
        name="triage",
        description="Issue triage, task hygiene, metadata updates, planning",
        title_keywords=[
            "triage",
            "task hygiene",
            "issue triage",
            "metadata",
            "task —",
            "close stale",
            "task cleanup",
            "planning",
        ],
        outcome_keywords=[
            "triaged",
            "closed",
            "task metadata",
            "updated task",
            "hygiene",
            "issue commented",
            "pr checked",
            "review audit",
        ],
        execution_keywords=[
            "closed issue",
            "updated task",
            "gptodo edit",
            "archived task",
            "task state",
            "metadata update",
        ],
        deliverable_keywords=[
            "triaged",
            "closed",
            "task metadata",
            "updated task",
            "hygiene",
            "issue commented",
        ],
    ),
    Category(
        name="strategic",
        description="Planning, design docs, architecture decisions, reviews",
        title_keywords=[
            "strategic:",
            "planning",
            "design doc",
            "architecture",
            "review —",
            "monthly review",
            "weekly review",
            "roadmap",
            "priorities",
        ],
        outcome_keywords=[
            "design doc",
            "architecture decision",
            "roadmap",
            "strategy",
            "plan created",
            "review complete",
        ],
        execution_keywords=[
            "design doc",
            "wrote design",
            "architecture",
            "reviewed",
            "analyzed",
            "strategy session",
        ],
        deliverable_keywords=[
            "design doc",
            "architecture",
            "strategy",
            "roadmap",
            "plan",
            "review",
        ],
    ),
    Category(
        name="content",
        description="Blog posts, documentation, knowledge articles",
        title_keywords=[
            "content:",
            "blog",
            "documentation",
            "knowledge",
            "article",
            "write-up",
            "post —",
            "docs:",
        ],
        outcome_keywords=[
            "blog post",
            "article published",
            "docs updated",
            "knowledge article",
            "documentation",
        ],
        execution_keywords=[
            "blog draft",
            "published",
            "documentation",
            "wrote article",
            "content pipeline",
        ],
        deliverable_keywords=[
            "blog post",
            "article",
            "documentation",
            "knowledge",
            "published",
        ],
    ),
    Category(
        name="cross-repo",
        description="Contributions to external repos (upstream, dependencies)",
        title_keywords=[
            "cross-repo",
            "upstream",
            "contribution",
            "external pr",
            "dependency",
        ],
        outcome_keywords=[
            "upstream pr",
            "contribution",
            "external fix",
            "dependency update",
        ],
        execution_keywords=[
            "upstream",
            "cross-repo",
            "external repo",
            "forked",
            "contributed",
        ],
        deliverable_keywords=[
            "upstream pr",
            "contribution",
            "external",
        ],
    ),
    Category(
        name="research",
        description="Investigation, analysis, exploration, learning",
        title_keywords=[
            "research:",
            "investigation",
            "analysis",
            "exploration",
            "learning",
            "experiment",
            "study",
        ],
        outcome_keywords=[
            "findings",
            "analysis complete",
            "research notes",
            "learned",
            "discovered",
            "explored",
        ],
        execution_keywords=[
            "investigated",
            "analyzed",
            "explored",
            "researched",
            "experimented",
            "studied",
            "benchmarked",
        ],
        deliverable_keywords=[
            "analysis",
            "findings",
            "research",
            "notes",
        ],
    ),
    Category(
        name="monitoring",
        description="PR monitoring, notification handling, CI checks",
        title_keywords=[
            "monitoring",
            "notification",
            "pr review",
            "ci check",
            "project monitoring",
        ],
        outcome_keywords=[
            "notifications handled",
            "prs reviewed",
            "ci checked",
            "monitoring complete",
        ],
        execution_keywords=[
            "checked notifications",
            "reviewed pr",
            "ci status",
            "monitoring run",
        ],
        deliverable_keywords=[
            "review comment",
            "notification handled",
            "pr reviewed",
        ],
    ),
    Category(
        name="self-review",
        description="Self-audit of infrastructure, lessons, task quality",
        title_keywords=[
            "self-review",
            "audit",
            "self-audit",
            "quality check",
            "lesson review",
        ],
        outcome_keywords=[
            "audit complete",
            "quality improved",
            "lessons updated",
            "review done",
        ],
        execution_keywords=[
            "audited",
            "reviewed",
            "quality check",
            "lesson validation",
            "self-review",
        ],
        deliverable_keywords=[
            "audit",
            "quality",
            "review",
            "validation",
        ],
    ),
    Category(
        name="social",
        description="Social engagement — tweets, replies, community interaction",
        title_keywords=[
            "social:",
            "tweet",
            "twitter",
            "discord",
            "community",
            "engagement",
        ],
        outcome_keywords=[
            "tweet posted",
            "reply sent",
            "community",
            "social engagement",
        ],
        execution_keywords=[
            "tweeted",
            "replied",
            "discord message",
            "social post",
        ],
        deliverable_keywords=[
            "tweet",
            "reply",
            "post",
            "social",
        ],
    ),
    Category(
        name="noop-hard",
        description="Nothing happened — error, timeout, empty session",
        title_keywords=[],
        outcome_keywords=[],
        execution_keywords=[],
        deliverable_keywords=[],
    ),
    Category(
        name="noop-soft",
        description="Journal written but no meaningful deliverables produced",
        title_keywords=[],
        outcome_keywords=[],
        execution_keywords=[],
        deliverable_keywords=[],
    ),
]


@dataclass
class ClassificationResult:
    """Result of session classification."""

    category: str
    confidence: float
    productive: bool
    classifier: str  # "llm" or "keyword"
    deliverables: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    secondary_category: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "category": self.category,
            "confidence": round(self.confidence, 3),
            "productive": self.productive,
            "classifier": self.classifier,
            "deliverables": self.deliverables,
            "blockers": self.blockers,
        }
        if self.secondary_category:
            d["secondary_category"] = self.secondary_category
        return d


# ──────────────────────────────────────────────────────────────────────
# Section extraction (from journal text)
# ──────────────────────────────────────────────────────────────────────


def _extract_sections(text: str) -> dict[str, str]:
    """Parse journal entry into structural sections for weighted scoring.

    Returns dict with keys: title, outcome, execution, deliverables, full.
    """
    sections: dict[str, str] = {
        "title": "",
        "outcome": "",
        "execution": "",
        "deliverables": "",
        "full": text,
    }

    lines = text.split("\n")

    # Title: first heading line
    for line in lines:
        if line.startswith("#"):
            sections["title"] = line.lstrip("#").strip()
            break

    # Outcome: YAML field if present
    m = re.search(r"^outcome:\s*(.+)$", text, re.MULTILINE)
    if m:
        sections["outcome"] = m.group(1).strip()

    # Execution section — various heading formats
    exec_patterns = [
        r"###?\s*(?:Step\s*3|Execution|Work)\b",
        r"##\s*Work:",
        r"##\s*Work\b",
    ]
    for pattern in exec_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            start = m.end()
            # Find next section heading
            next_heading = re.search(r"\n##", text[start:])
            end = start + next_heading.start() if next_heading else len(text)
            sections["execution"] = text[start:end].strip()
            break

    # Deliverables section
    deliv_patterns = [
        r"###?\s*Deliverables?\b",
        r"##\s*Work Completed\b",
        r"###?\s*Summary\b",
        r"##\s*Commits?\b",
    ]
    for pattern in deliv_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            start = m.end()
            next_heading = re.search(r"\n##", text[start:])
            end = start + next_heading.start() if next_heading else len(text)
            sections["deliverables"] = text[start:end].strip()
            break

    return sections


# ──────────────────────────────────────────────────────────────────────
# Keyword classifier
# ──────────────────────────────────────────────────────────────────────


def _score_text(text: str, keywords: list[str]) -> float:
    """Count keyword matches in text (case-insensitive)."""
    if not text or not keywords:
        return 0.0
    text_lower = text.lower()
    return sum(1.0 for kw in keywords if kw.lower() in text_lower)


def _extract_deliverables(text: str) -> list[str]:
    """Extract deliverable items from deliverables section."""
    deliverables = []
    # Bullet items
    for m in re.finditer(r"^[\s]*[-*]\s+(.+)$", text, re.MULTILINE):
        item = m.group(1).strip()
        if item and len(item) > 5:
            deliverables.append(item)
    # Numbered subsections
    if not deliverables:
        for m in re.finditer(r"###?\s+(?:\d+\.\s+)?(.+)$", text, re.MULTILINE):
            item = m.group(1).strip()
            if item and len(item) > 5:
                deliverables.append(item)
    return deliverables


def _extract_blockers(text: str) -> list[str]:
    """Extract blocker descriptions from journal text."""
    blockers = []
    patterns = [
        r"blocked\s+on\s+(.+?)(?:\.|$)",
        r"waiting\s+for\s+(.+?)(?:\.|$)",
        r"blockers?:\s*(.+?)(?:\.|$)",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            blocker = m.group(1).strip()
            if blocker and blocker not in blockers:
                blockers.append(blocker)
    return blockers[:5]


def classify_by_keywords(
    journal_text: str,
    categories: list[Category] | None = None,
) -> ClassificationResult:
    """Classify a session using section-aware weighted keyword matching.

    Args:
        journal_text: The session journal entry text.
        categories: Category definitions with keyword signals.
            Defaults to DEFAULT_CATEGORIES if not provided.

    Returns:
        ClassificationResult with category, confidence, and extracted metadata.
    """
    if categories is None:
        categories = DEFAULT_CATEGORIES

    sections = _extract_sections(journal_text)

    # Score each category across sections with weights
    scores: dict[str, float] = {}
    for cat in categories:
        if cat.name.startswith("noop"):
            continue  # NOOPs are detected by absence, not keywords
        title_score = _score_text(sections["title"], cat.title_keywords)
        outcome_score = _score_text(sections["outcome"], cat.outcome_keywords)
        exec_score = _score_text(sections["execution"], cat.execution_keywords)
        deliv_score = _score_text(sections["deliverables"], cat.deliverable_keywords)

        combined = title_score * 3.0 + outcome_score * 2.0 + deliv_score * 2.0 + exec_score * 1.5
        if combined > 0:
            scores[cat.name] = combined

    # Check for explicit category label in text (YAML field or title prefix)
    cat_names = {c.name for c in categories}
    # Aliases for common variations
    _aliases: dict[str, str] = {
        "bug-fix": "code",
        "bugfix": "code",
        "feature": "code",
        "infra": "infrastructure",
        "ops": "infrastructure",
        "docs": "content",
        "documentation": "content",
        "planning": "strategic",
        "design": "strategic",
        "review": "self-review",
        "audit": "self-review",
    }

    explicit_cat = None
    # From YAML category field
    m = re.search(r"^category:\s*(\S+)", journal_text, re.MULTILINE)
    if m:
        label = m.group(1).strip().lower()
        explicit_cat = label if label in cat_names else _aliases.get(label)
    # From title prefix (e.g., "Code: ..." or "Strategic —")
    if not explicit_cat:
        m = re.match(r"#*\s*(\w[\w-]*)[\s:—-]", sections["title"])
        if m:
            label = m.group(1).strip().lower()
            explicit_cat = label if label in cat_names else _aliases.get(label)

    if explicit_cat:
        scores[explicit_cat] = scores.get(explicit_cat, 0) + 10.0

    # Extract deliverables and blockers
    deliverables = _extract_deliverables(sections.get("deliverables", ""))
    if not deliverables:
        deliverables = _extract_deliverables(sections.get("execution", ""))
    blockers = _extract_blockers(journal_text)

    # Determine category
    if not scores:
        # No keyword signal — check for NOOP
        if len(journal_text.strip()) < 200 and not deliverables:
            return ClassificationResult(
                category="noop-hard",
                confidence=0.9,
                productive=False,
                classifier="keyword",
                deliverables=[],
                blockers=blockers,
            )
        return ClassificationResult(
            category="noop-soft",
            confidence=0.5,
            productive=False,
            classifier="keyword",
            deliverables=deliverables,
            blockers=blockers,
        )

    sorted_cats = sorted(scores.items(), key=lambda x: -x[1])
    best_cat, best_score = sorted_cats[0]
    secondary = sorted_cats[1] if len(sorted_cats) > 1 and sorted_cats[1][1] >= 1.5 else None

    # Confidence from score magnitude
    if best_score >= 3.0:
        confidence = min(0.95, 0.5 + best_score * 0.05)
    elif best_score >= 1.5:
        confidence = 0.4 + best_score * 0.05
    elif best_score >= 1.0:
        confidence = 0.35
    else:
        confidence = 0.3

    # Determine productivity
    productive_cats = cat_names - {"noop-hard", "noop-soft"}
    productive = best_cat in productive_cats and bool(deliverables)

    # Promotion: noop-soft with deliverables → infer real category
    if not productive and deliverables:
        deliv_text = " ".join(deliverables).lower()
        if any(kw in deliv_text for kw in ("pr", "fix", "bug", "commit", "code")):
            best_cat = "code"
            productive = True
        elif any(kw in deliv_text for kw in ("triage", "issue", "close")):
            best_cat = "triage"
            productive = True
        elif any(kw in deliv_text for kw in ("blog", "post", "article", "content")):
            best_cat = "content"
            productive = True
        elif any(kw in deliv_text for kw in ("ci", "infra", "deploy", "monitor", "config")):
            best_cat = "infrastructure"
            productive = True
        elif scores:
            # Generic evidence of work
            best_cat = sorted_cats[0][0]
            productive = True

    return ClassificationResult(
        category=best_cat,
        confidence=confidence,
        productive=productive,
        classifier="keyword",
        deliverables=deliverables,
        blockers=blockers,
        secondary_category=secondary[0] if secondary else None,
    )


# ──────────────────────────────────────────────────────────────────────
# LLM classifier (reuses judge infrastructure)
# ──────────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """\
You are classifying an AI agent's work session into a category.
Return ONLY a JSON object with keys:
  "category" (string — one of the valid categories),
  "confidence" (float 0.0-1.0),
  "productive" (boolean — did the session produce meaningful output?),
  "deliverables" (list of strings — concrete outputs like PRs, commits, docs),
  "blockers" (list of strings — what blocked progress, if anything).
Do not wrap in markdown code blocks."""

_CLASSIFY_PROMPT_TEMPLATE = """\
## Valid Categories
{categories}

## Session Journal
{journal}

## Instructions
Classify this session into exactly ONE of the valid categories above.
- Prefer specific categories over generic ones
- "noop-hard" = nothing happened (error, timeout, empty)
- "noop-soft" = journal written but no meaningful deliverables
- Extract concrete deliverables (e.g., "PR gptme#1234", "design doc for Y")
- Extract blockers (e.g., "waiting for review on PR #123")
- Confidence should reflect how clearly the session fits the category

Return JSON: {{"category": "<category>", "confidence": <float>, "productive": <bool>, "deliverables": [...], "blockers": [...]}}"""


def classify_by_llm(
    journal_text: str,
    categories: list[Category] | None = None,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> ClassificationResult | None:
    """Classify a session using an LLM.

    Args:
        journal_text: The session journal entry text.
        categories: Category definitions. Defaults to DEFAULT_CATEGORIES.
        model: Anthropic model ID. Defaults to Haiku for cost efficiency.
        api_key: Anthropic API key. Falls back to env/config if not provided.

    Returns:
        ClassificationResult on success, None on failure (missing API key, etc.).
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; LLM classifier unavailable")
        return None

    from .judge import DEFAULT_JUDGE_MODEL, _get_api_key

    if categories is None:
        categories = DEFAULT_CATEGORIES
    if model is None:
        model = DEFAULT_JUDGE_MODEL

    key = api_key or _get_api_key()
    if not key:
        logger.warning("No Anthropic API key found; LLM classifier unavailable")
        return None

    # Build category descriptions for prompt
    cat_lines = []
    for cat in categories:
        cat_lines.append(f"- **{cat.name}**: {cat.description}")
    cat_text = "\n".join(cat_lines)

    # Truncate journal for cost control
    truncated = journal_text[:4000] if journal_text else "(empty session)"

    prompt = _CLASSIFY_PROMPT_TEMPLATE.format(
        categories=cat_text,
        journal=truncated,
    )

    cat_names = {c.name for c in categories}

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = getattr(response.content[0], "text", "").strip()

        # Handle markdown code block wrapping
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        verdict = json.loads(text)

        category = str(verdict.get("category", ""))
        if category not in cat_names:
            logger.warning("LLM returned unknown category %r; falling back", category)
            return None

        confidence = float(verdict.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return ClassificationResult(
            category=category,
            confidence=confidence,
            productive=bool(verdict.get("productive", False)),
            classifier="llm",
            deliverables=list(verdict.get("deliverables", [])),
            blockers=list(verdict.get("blockers", [])),
        )

    except Exception as e:
        logger.warning("LLM classifier failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────
# Combined judge+classify (single LLM call for both score and category)
# ──────────────────────────────────────────────────────────────────────

_JUDGE_CLASSIFY_SYSTEM = """\
You are evaluating AND classifying an AI agent's work session.
Return ONLY a JSON object with keys:
  "category" (string — one of the valid categories),
  "score" (float 0.0-1.0 — strategic goal-alignment score),
  "reason" (string — 1-sentence scoring explanation),
  "productive" (boolean),
  "deliverables" (list of strings),
  "blockers" (list of strings).
Do not wrap in markdown code blocks."""

_JUDGE_CLASSIFY_PROMPT = """\
## Agent Goals (ordered by priority)
{goals}

## Valid Categories
{categories}

## Session Journal
{journal}

## Instructions
1. **Classify** this session into exactly ONE of the valid categories.
2. **Score** the strategic value (0.0-1.0):
   - 0.9-1.0: Major progress on top-priority goal
   - 0.7-0.8: Meaningful progress on priority goal
   - 0.5-0.6: Useful but not top-priority work
   - 0.3-0.4: Low-value work
   - 0.1-0.2: Minimal output
   - 0.0: No output / pure NOOP

Key: ONE impactful deliverable > FIVE small deliverables.

Return JSON: {{"category": "<cat>", "score": <float>, "reason": "<1 sentence>", "productive": <bool>, "deliverables": [...], "blockers": [...]}}"""


def judge_and_classify(
    journal_text: str,
    categories: list[Category] | None = None,
    *,
    goals: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[ClassificationResult | None, dict | None]:
    """Classify AND score a session in a single LLM call.

    Returns a tuple of (ClassificationResult, judge_dict) where judge_dict
    has keys ``score``, ``reason``, ``model``. Either or both may be None
    on failure.
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed")
        return None, None

    from .judge import DEFAULT_GOALS, DEFAULT_JUDGE_MODEL, _get_api_key

    if categories is None:
        categories = DEFAULT_CATEGORIES
    if model is None:
        model = DEFAULT_JUDGE_MODEL
    if goals is None:
        goals = DEFAULT_GOALS

    key = api_key or _get_api_key()
    if not key:
        logger.warning("No Anthropic API key found")
        return None, None

    cat_lines = [f"- **{c.name}**: {c.description}" for c in categories]
    cat_text = "\n".join(cat_lines)
    truncated = journal_text[:4000] if journal_text else "(empty session)"

    prompt = _JUDGE_CLASSIFY_PROMPT.format(
        goals=goals,
        categories=cat_text,
        journal=truncated,
    )

    cat_names = {c.name for c in categories}

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=350,
            system=_JUDGE_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = getattr(response.content[0], "text", "").strip()

        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        verdict = json.loads(text)

        # Extract classification
        category = str(verdict.get("category", ""))
        if category not in cat_names:
            logger.warning("LLM returned unknown category %r", category)
            return None, None

        classification = ClassificationResult(
            category=category,
            confidence=max(0.0, min(1.0, float(verdict.get("confidence", 0.7)))),
            productive=bool(verdict.get("productive", False)),
            classifier="llm",
            deliverables=list(verdict.get("deliverables", [])),
            blockers=list(verdict.get("blockers", [])),
        )

        # Extract judge score
        score = max(0.0, min(1.0, float(verdict.get("score", 0.5))))
        judge_result = {
            "score": score,
            "reason": str(verdict.get("reason", "")),
            "model": model,
        }

        return classification, judge_result

    except Exception as e:
        logger.warning("LLM judge+classify failed: %s", e)
        return None, None


# ──────────────────────────────────────────────────────────────────────
# Hybrid classifier (public API)
# ──────────────────────────────────────────────────────────────────────


def classify_session(
    journal_text: str,
    categories: list[Category] | None = None,
    *,
    use_llm: bool = True,
    model: str | None = None,
    api_key: str | None = None,
) -> ClassificationResult:
    """Classify a session using hybrid approach: LLM primary, keyword fallback.

    Args:
        journal_text: The session journal entry text.
        categories: Category definitions. Defaults to DEFAULT_CATEGORIES.
        use_llm: Whether to try LLM classification first (default True).
        model: Anthropic model ID for LLM classifier.
        api_key: Anthropic API key.

    Returns:
        ClassificationResult — always returns a result (keyword fallback is deterministic).
    """
    if use_llm:
        result = classify_by_llm(journal_text, categories, model=model, api_key=api_key)
        if result is not None:
            return result
        logger.info("LLM classification failed; falling back to keyword classifier")

    return classify_by_keywords(journal_text, categories)
