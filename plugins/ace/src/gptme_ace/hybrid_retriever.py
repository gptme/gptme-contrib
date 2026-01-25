"""Hybrid lesson retrieval system combining keyword, semantic, effectiveness, and recency scoring.

Implements Phase 5.2 of ACE context optimization.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import math

import numpy as np


@dataclass
class HybridConfig:
    """Configuration for hybrid retrieval scoring."""

    # Weights (must sum to ~1.0 before tool bonus)
    keyword_weight: float = 0.25
    semantic_weight: float = 0.40
    effectiveness_weight: float = 0.25
    recency_weight: float = 0.10
    tool_bonus: float = 0.20

    # Recency parameters
    recency_decay_days: float = 30.0  # Half-life ~21 days

    # Retrieval parameters
    top_k_candidates: int = 20  # Stage 1: candidate filtering
    top_n_results: int = 5  # Stage 2: final results


def keyword_score(lesson: Any, keywords: list[str]) -> float:
    """Compute normalized keyword relevance score (0.0-1.0).

    Args:
        lesson: Lesson with metadata.keywords
        keywords: Query keywords to match

    Returns:
        Score normalized by total keywords (0.0-1.0)
    """
    if not lesson.metadata.keywords:
        return 0.0

    matches = sum(1 for kw in lesson.metadata.keywords if kw in keywords)
    return matches / max(len(lesson.metadata.keywords), 1)


def semantic_score(query_embed: np.ndarray, lesson_embed: np.ndarray) -> float:
    """Compute cosine similarity between query and lesson embeddings (0.0-1.0).

    Args:
        query_embed: Query embedding vector
        lesson_embed: Lesson embedding vector

    Returns:
        Cosine similarity normalized to [0.0, 1.0]
    """
    # Compute cosine similarity
    cosine = np.dot(query_embed, lesson_embed) / (
        np.linalg.norm(query_embed) * np.linalg.norm(lesson_embed)
    )
    # Normalize from [-1, 1] to [0, 1]
    return float((cosine + 1.0) / 2.0)


def effectiveness_score(metadata: dict[str, Any]) -> float:
    """Compute effectiveness score from lesson metadata (0.0-1.0).

    Formula: helpful_count / (helpful_count + harmful_count + 1)

    Args:
        metadata: Lesson metadata containing usage_count, helpful_count, harmful_count

    Returns:
        Effectiveness score (0.5 for new lessons, 0.0-1.0 based on usage)
    """
    helpful: int = metadata.get("helpful_count", 0)
    harmful: int = metadata.get("harmful_count", 0)

    # Default 0.5 for lessons without feedback
    if helpful == 0 and harmful == 0:
        return 0.5

    return float(helpful / (helpful + harmful + 1))


def recency_score(last_updated: datetime, decay_days: float = 30.0) -> float:
    """Compute recency score with exponential decay (0.0-1.0).

    Formula: exp(-days_since_update / decay_constant)

    Args:
        last_updated: When lesson was last updated
        decay_days: Decay constant (half-life ~21 days for 30)

    Returns:
        Recency score (1.0 for just updated, decays to 0)
    """
    days_since = (datetime.now() - last_updated).days
    return math.exp(-days_since / decay_days)


def tool_bonus(lesson_tools: list[str], context_tools: list[str]) -> float:
    """Compute tool match bonus (0.0 or 0.2).

    Args:
        lesson_tools: Tools listed in lesson metadata
        context_tools: Tools used in current context

    Returns:
        0.2 if any tool matches, 0.0 otherwise
    """
    if not lesson_tools or not context_tools:
        return 0.0

    matches = any(tool in context_tools for tool in lesson_tools)
    return 0.2 if matches else 0.0


class HybridLessonMatcher:
    """Hybrid lesson matcher combining keyword, semantic, effectiveness, and recency scoring."""

    def __init__(self, embedder: Any, config: HybridConfig | None = None):
        """Initialize hybrid matcher.

        Args:
            embedder: LessonEmbedder instance for semantic scoring
            config: Configuration (uses defaults if None)
        """
        self.embedder = embedder
        self.config = config or HybridConfig()

    def get_candidates(
        self,
        lessons: list[Any],
        keywords: list[str],
        tools: list[str],
        top_k: int | None = None,
    ) -> list[Any]:
        """Stage 1: Fast candidate filtering by keyword/tool.

        Args:
            lessons: All available lessons
            keywords: Query keywords
            tools: Context tools
            top_k: Number of candidates (uses config if None)

        Returns:
            Top-K lessons by keyword+tool score
        """
        top_k = top_k or self.config.top_k_candidates

        # Score each lesson by keyword + tool
        scored = []
        for lesson in lessons:
            kw_score = keyword_score(lesson, keywords)
            tb_score = tool_bonus(lesson.metadata.tools, tools)
            combined = kw_score + tb_score
            scored.append((combined, lesson))

        # Sort descending and take top-K
        scored.sort(reverse=True, key=lambda x: x[0])
        return [lesson for _, lesson in scored[:top_k]]

    def score_candidates(
        self,
        candidates: list[Any],
        query: str,
        keywords: list[str],
        tools: list[str],
    ) -> list[tuple[float, Any]]:
        """Stage 2: Precise hybrid scoring on candidates.

        Args:
            candidates: Filtered candidate lessons
            query: User query text
            keywords: Query keywords
            tools: Context tools

        Returns:
            List of (score, lesson) tuples sorted by score descending
        """
        # Generate query embedding
        query_embed = self.embedder.generate_embedding(query)

        # Score each candidate
        results = []
        for lesson in candidates:
            # Get lesson ID and metadata
            lesson_id = self.embedder._lesson_to_id(lesson.path)
            metadata = self.embedder.metadata.get(lesson_id, {})

            # Skip if no embedding
            if lesson_id not in self.embedder.metadata:
                continue

            # Get embedding from FAISS index
            idx = list(self.embedder.metadata.keys()).index(lesson_id)
            lesson_embed = self.embedder.index.reconstruct(idx)

            # Compute all 5 components
            kw = keyword_score(lesson, keywords)
            sem = semantic_score(query_embed, lesson_embed)
            eff = effectiveness_score(metadata)

            # Recency from metadata
            last_update = metadata.get("updated", datetime.now())
            if isinstance(last_update, str):
                last_update = datetime.fromisoformat(last_update)
            rec = recency_score(last_update, self.config.recency_decay_days)

            tb = tool_bonus(lesson.metadata.tools, tools)

            # Weighted combination
            hybrid = (
                self.config.keyword_weight * kw
                + self.config.semantic_weight * sem
                + self.config.effectiveness_weight * eff
                + self.config.recency_weight * rec
                + tb
            )

            results.append((hybrid, lesson))

        # Sort by score descending
        results.sort(reverse=True, key=lambda x: x[0])
        return results

    def match(
        self,
        lessons: list[Any],
        query: str,
        keywords: list[str],
        tools: list[str] | None = None,
        top_n: int | None = None,
    ) -> list[tuple[float, Any]]:
        """Two-stage hybrid retrieval.

        Args:
            lessons: All available lessons
            query: User query text
            keywords: Query keywords
            tools: Context tools (optional)
            top_n: Number of results (uses config if None)

        Returns:
            Top-N lessons by hybrid score as (score, lesson) tuples
        """
        tools = tools or []
        top_n = top_n or self.config.top_n_results

        # Stage 1: Candidate filtering
        candidates = self.get_candidates(lessons, keywords, tools)

        # Stage 2: Hybrid scoring
        scored = self.score_candidates(candidates, query, keywords, tools)

        # Return top-N
        return scored[:top_n]
