"""Lightweight, regex-based "LLM smell" detector for session journal prose.

This is a stdlib-only, agent-agnostic heuristic that scans text for recognizable
tells of bland AI output (hedging filler, ChatGPT vocabulary tics, negative
parallelism, canned enthusiasm, em-dash abuse). The intent is a low-cost supplement
to the LLM-judge session-quality signal — it catches a stylistic dimension (blandness /
voice drift) that a judge trained on the same distribution tends to overlook.

Exposed as :func:`detect_smells` for inline use during :func:`post_session`, and as
:func:`compute_smell_score` for the one-call wrapper used by external callers.

Originally extracted from Bob-local ``scripts/analysis/llm_smell_detector.py``
(idea backlog #382, Phase 1).  This copy lives in gptme-sessions so any agent
workspace can compute a smell score at post-session time without a Bob-relative import.

Scoring: each pattern carries a weight reflecting how strongly it signals
machine-generated blandness. High-confidence tells ("delve", "tapestry",
canned openers, negative parallelism) are weighted heavily; words that also
appear in legitimate technical writing ("robust", "leverage", "crucial") are
weighted low so normal engineering prose does not score as smelly. The headline
``weighted_score`` is weighted hits per 1000 words, so it is comparable across
texts of different lengths.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

# (category, weight, regex_source, label)
# weight: 3 = high-confidence tell, 2 = moderate, 1 = soft (overlaps legit prose)
_RAW_PATTERNS: list[tuple[str, int, str, str]] = [
    # --- Hedging / filler openers (high confidence) ---
    (
        "hedging",
        3,
        r"\bit(?:'s| is) worth (?:noting|mentioning)\b",
        "it's worth noting",
    ),
    (
        "hedging",
        3,
        r"\bit(?:'s| is) important to (?:note|remember|understand)\b",
        "it's important to note",
    ),
    ("hedging", 2, r"\bneedless to say\b", "needless to say"),
    ("hedging", 2, r"\bthat (?:being |)said,?\b", "that said"),
    ("hedging", 2, r"\bat the end of the day\b", "at the end of the day"),
    # --- ChatGPT vocabulary tics (high confidence) ---
    ("vocab_strong", 3, r"\bdelv(?:e|ing|es)\b", "delve"),
    ("vocab_strong", 3, r"\btapestr(?:y|ies)\b", "tapestry"),
    ("vocab_strong", 3, r"\b(?:a |)testament to\b", "testament to"),
    ("vocab_strong", 3, r"\bin the realm of\b", "in the realm of"),
    (
        "vocab_strong",
        3,
        r"\bnavigat(?:e|ing) the (?:complexit|landscape|intricac)",
        "navigate the complexities",
    ),
    ("vocab_strong", 3, r"\bever-(?:evolving|changing|growing)\b", "ever-evolving"),
    ("vocab_strong", 2, r"\b(?:rich |)tapestry of\b", "tapestry of"),
    ("vocab_strong", 2, r"\bunderscor(?:e|es|ing)\b", "underscore"),
    ("vocab_strong", 2, r"\bshowcas(?:e|es|ing)\b", "showcase"),
    ("vocab_strong", 2, r"\bboast(?:s|ing|)\b", "boasts"),
    ("vocab_strong", 2, r"\bgame[- ]chang(?:er|ing)\b", "game-changer"),
    # --- Soft vocab (overlaps legitimate technical writing) ---
    ("vocab_soft", 1, r"\bleverag(?:e|es|ing)\b", "leverage"),
    ("vocab_soft", 1, r"\brobust\b", "robust"),
    ("vocab_soft", 1, r"\bseamless(?:ly|)\b", "seamless"),
    ("vocab_soft", 1, r"\bcrucial\b", "crucial"),
    ("vocab_soft", 1, r"\bpivotal\b", "pivotal"),
    ("vocab_soft", 1, r"\bcomprehensive\b", "comprehensive"),
    ("vocab_soft", 1, r"\bintricate\b", "intricate"),
    ("vocab_soft", 1, r"\bmeticulous(?:ly|)\b", "meticulous"),
    # --- Negative parallelism (high confidence) ---
    (
        "parallelism",
        3,
        r"\bit(?:'s| is) not just .{1,40}?,? (?:it(?:'s| is)|but)\b",
        "it's not just X, it's Y",
    ),
    (
        "parallelism",
        3,
        r"\bisn(?:'t| not) (?:merely|just) .{1,40}? but\b",
        "isn't merely X but Y",
    ),
    ("parallelism", 2, r"\bnot only .{1,40}? but (?:also|)\b", "not only X but also Y"),
    # --- Canned enthusiasm / assistant voice (high confidence) ---
    (
        "enthusiasm",
        3,
        r"(?:^|\.\s+)(?:Certainly|Absolutely|Of course)[!,]",
        "Certainly!/Absolutely!",
    ),
    ("enthusiasm", 3, r"\bgreat question\b", "great question"),
    ("enthusiasm", 3, r"\bI(?:'d| would) be (?:happy|glad) to\b", "I'd be happy to"),
    ("enthusiasm", 2, r"\bI hope this helps\b", "I hope this helps"),
    ("enthusiasm", 2, r"\bdive (?:in|into)\b", "dive in"),
    # --- Canned conclusions ---
    ("conclusion", 2, r"\bin conclusion\b", "in conclusion"),
    ("conclusion", 2, r"\bin summary\b", "in summary"),
    ("conclusion", 2, r"\bto sum (?:up|it up)\b", "to sum up"),
    ("conclusion", 1, r"(?:^|\n)\s*Overall,", "Overall,"),
    # --- Transition overuse (soft; counted, density matters) ---
    ("transition", 1, r"(?:^|\n)\s*Moreover,", "Moreover,"),
    ("transition", 1, r"(?:^|\n)\s*Furthermore,", "Furthermore,"),
    ("transition", 1, r"(?:^|\n)\s*Additionally,", "Additionally,"),
]

_COMPILED = [
    (cat, weight, re.compile(src, re.IGNORECASE | re.MULTILINE), label)
    for cat, weight, src, label in _RAW_PATTERNS
]

_EM_DASH = re.compile(r"\s—\s|\w—\w|—")
_WORD = re.compile(r"\b\w+\b")


def detect_smells(text: str) -> dict:
    """Scan *text* for LLM smells and return a structured report.

    Returns a dict with:
      word_count, total_hits, weighted_score (weighted hits / 1000 words),
      em_dash_count, em_dash_per_1k, by_category, hits (sorted desc).
    """
    word_count = len(_WORD.findall(text))
    hits: list[dict] = []
    by_category: dict[str, int] = {}
    total_hits = 0
    weighted_total = 0

    for cat, weight, rx, label in _COMPILED:
        n = len(rx.findall(text))
        if n:
            hits.append({"category": cat, "label": label, "count": n, "weight": weight})
            by_category[cat] = by_category.get(cat, 0) + n
            total_hits += n
            weighted_total += n * weight

    em_dash_count = len(_EM_DASH.findall(text))
    per_1k = (1000.0 / word_count) if word_count else 0.0
    tolerated = word_count / 1000.0
    em_excess = max(0, em_dash_count - round(tolerated))
    weighted_total += em_excess  # weight 1
    if em_excess:
        hits.append(
            {
                "category": "em_dash",
                "label": "em-dash abuse",
                "count": em_excess,
                "weight": 1,
            }
        )
        by_category["em_dash"] = em_excess
        total_hits += em_excess

    hits.sort(key=lambda h: (h["count"] * h["weight"]), reverse=True)

    return {
        "word_count": word_count,
        "total_hits": total_hits,
        "weighted_score": round(weighted_total * per_1k, 2),
        "em_dash_count": em_dash_count,
        "em_dash_per_1k": round(em_dash_count * per_1k, 2),
        "by_category": by_category,
        "hits": hits,
    }


def compute_smell_score(journal_path: str | Path) -> float | None:
    """Read *journal_path* and return a scaled smell score (0.0–1.0).

    Returns ``None`` if the file cannot be read or is empty.  The raw
    ``weighted_score`` is sigmoid-scaled: a score of 0 → 0.0 (clean voice),
    ~10 → ~0.5, ≥30 → ~1.0.
    """
    try:
        text = Path(journal_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    report = detect_smells(text)
    raw = report["weighted_score"]
    # Sigmoid: map raw weighted_score to 0.0-1.0 range.
    #  0    → ~0.0  (clean technical prose)
    #  10   → ~0.5  (moderate)
    #  30   → ~0.95 (heavy LLM voice)
    scaled = 1.0 / (1.0 + math.exp(-(raw - 10) / 6.0))
    return round(scaled, 4)
