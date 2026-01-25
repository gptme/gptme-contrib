"""
ACE integration with gptme's lesson system.

Provides GptmeHybridMatcher as a drop-in replacement for gptme's LessonMatcher,
using ACE's hybrid retrieval algorithm for improved lesson selection.
"""

import logging
import os
from typing import Any


from .embedder import LessonEmbedder
from .hybrid_retriever import HybridLessonMatcher, HybridConfig
from .retrieval_analytics import RetrievalTracker

logger = logging.getLogger(__name__)


class GptmeHybridMatcher:
    """
    Hybrid lesson matcher compatible with gptme's LessonMatcher interface.

    Uses ACE's hybrid retrieval algorithm (keyword + semantic + effectiveness + recency)
    instead of simple keyword matching. Falls back to keyword-only if embeddings unavailable.

    Example:
        # Drop-in replacement for LessonMatcher
        matcher = GptmeHybridMatcher()
        results = matcher.match(lessons, context, threshold=0.5)
    """

    def __init__(
        self,
        embedder: LessonEmbedder | None = None,
        config: HybridConfig | None = None,
        tracker: RetrievalTracker | None = None,
    ):
        """
        Initialize hybrid matcher.

        Args:
            embedder: Optional lesson embedder for semantic matching
            config: Optional hybrid configuration (uses defaults if not provided)
            tracker: Optional retrieval tracker for analytics
        """
        self.embedder = embedder
        self.config = config or HybridConfig()
        self.matcher = HybridLessonMatcher(embedder, self.config) if embedder else None
        self.tracker = tracker or RetrievalTracker()

        # Check if hybrid mode is enabled via environment
        self.hybrid_enabled = os.getenv("GPTME_LESSONS_HYBRID", "false").lower() in (
            "true",
            "1",
            "yes",
        )

        if self.hybrid_enabled and self.embedder is None:
            logger.warning(
                "Hybrid matching enabled but no embedder provided, "
                "will fall back to keyword matching"
            )

    def match(
        self,
        lessons: list[Any],
        context: Any,
        threshold: float = 0.0,
        session_id: str | None = None,
    ) -> list[Any]:
        """
        Find matching lessons and score them using hybrid retrieval.

        Args:
            lessons: List of gptme Lesson objects
            context: gptme MatchContext with message and tools_used
            threshold: Minimum score threshold (0.0-1.0)
            session_id: Optional session ID for analytics tracking

        Returns:
            List of gptme MatchResult objects, sorted by score descending
        """
        # If hybrid disabled or matcher unavailable, fall back to keyword matching
        if not self.hybrid_enabled or self.matcher is None:
            results = self._keyword_fallback(lessons, context, threshold)
            method = "keyword"
        else:
            results = self._hybrid_match(lessons, context, threshold)
            method = "hybrid"

        # Log retrieval event if session_id provided
        if session_id and self.tracker:
            self._log_retrieval(session_id, context, results, method)

        return results

    def _hybrid_match(
        self, lessons: list[Any], context: Any, threshold: float
    ) -> list[Any]:
        """
        Perform hybrid retrieval matching.

        Args:
            lessons: List of gptme Lesson objects
            context: gptme MatchContext
            threshold: Minimum score threshold

        Returns:
            List of gptme MatchResult objects
        """
        # Type guard - should never be None when called
        if self.matcher is None:
            return []

        # Prepare query context
        query = context.message
        active_tools = context.tools_used or []

        # Extract keywords from message (simple word extraction)
        keywords = [
            word.lower()
            for word in query.split()
            if len(word) > 3  # Skip short words
        ]

        # Retrieve lessons using hybrid algorithm
        # Returns list of (score, lesson) tuples
        scored_lessons = self.matcher.match(
            lessons=lessons,
            query=query,
            keywords=keywords,
            tools=active_tools,
        )

        # Filter by threshold and convert to gptme format
        gptme_results = []
        for score, lesson in scored_lessons:
            if score >= threshold:
                matched_by = [f"hybrid:{score:.2f}"]
                gptme_results.append(
                    self._create_match_result(lesson, score, matched_by)
                )

        return gptme_results

    def _log_retrieval(
        self,
        session_id: str,
        context: Any,
        results: list[Any],
        method: str,
    ) -> None:
        """
        Log retrieval event for analytics.

        Args:
            session_id: Session identifier
            context: gptme MatchContext with message and tools
            results: List of gptme MatchResult objects
            method: Retrieval method used ("keyword" or "hybrid")
        """
        # Convert results to dict format for logging
        lessons_data = []
        for result in results:
            # Safely access lesson ID with multiple fallbacks
            lesson_id = "unknown"
            if (
                hasattr(result.lesson, "metadata")
                and result.lesson.metadata is not None
            ):
                lesson_id = (
                    getattr(result.lesson.metadata, "id", None)
                    or getattr(result.lesson.metadata, "lesson_id", None)
                    or "unknown"
                )
            elif hasattr(result.lesson, "path"):
                # Fall back to path-based ID
                lesson_id = str(getattr(result.lesson, "path", "unknown"))
            lesson_data = {
                "id": lesson_id,
                "score": result.score,
                "matched_by": result.matched_by,
            }
            lessons_data.append(lesson_data)

        # Log the retrieval event
        self.tracker.log_retrieval(
            session_id=session_id,
            query=context.message,
            lessons=lessons_data,
            method=method,  # type: ignore
            top_n=len(results),
            token_count=None,  # Could add token counting later
        )

    def _keyword_fallback(
        self, lessons: list[Any], context: Any, threshold: float
    ) -> list[Any]:
        """
        Fallback to keyword-only matching when hybrid unavailable.

        Uses the same logic as gptme's original LessonMatcher.
        """
        results = []
        message_lower = context.message.lower()

        for lesson in lessons:
            score = 0.0
            matched_by = []

            # Keyword matching
            for keyword in lesson.metadata.keywords:
                if keyword.lower() in message_lower:
                    score += 1.0
                    matched_by.append(f"keyword:{keyword}")

            # Tool matching
            if context.tools_used and lesson.metadata.tools:
                for tool in lesson.metadata.tools:
                    if tool in context.tools_used:
                        score += 2.0
                        matched_by.append(f"tool:{tool}")

            if score > threshold:
                results.append(self._create_match_result(lesson, score, matched_by))

        # Sort by score, descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _create_match_result(
        self, lesson: Any, score: float, matched_by: list[str]
    ) -> Any:
        """
        Create gptme MatchResult from components.

        Args:
            lesson: gptme Lesson object
            score: Match score
            matched_by: List of match reasons

        Returns:
            gptme MatchResult object
        """
        # Import here to avoid circular dependency issues
        try:
            from gptme.lessons.matcher import (
                MatchResult as GptmeMatchResult,
            )

            return GptmeMatchResult(lesson=lesson, score=score, matched_by=matched_by)
        except ImportError:
            # If gptme not available, create a simple dataclass
            from dataclasses import dataclass

            @dataclass
            class FallbackMatchResult:
                lesson: Any
                score: float
                matched_by: list[str]

            return FallbackMatchResult(
                lesson=lesson, score=score, matched_by=matched_by
            )

    def match_keywords(self, lessons: list[Any], keywords: list[str]) -> list[Any]:
        """
        Match lessons by explicit keywords.

        Provided for compatibility with gptme's LessonMatcher interface.
        Uses keyword-only matching regardless of hybrid mode.

        Args:
            lessons: List of gptme Lesson objects
            keywords: Keywords to match

        Returns:
            List of gptme MatchResult objects
        """
        results = []

        for lesson in lessons:
            matched_keywords = [kw for kw in keywords if kw in lesson.metadata.keywords]

            if matched_keywords:
                score = float(len(matched_keywords))
                matched_by = [f"keyword:{kw}" for kw in matched_keywords]
                results.append(self._create_match_result(lesson, score, matched_by))

        results.sort(key=lambda r: r.score, reverse=True)
        return results
