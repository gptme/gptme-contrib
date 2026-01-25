"""Retrieval analytics for ACE hybrid lesson matching.

This module provides tracking, metrics, and A/B testing for lesson retrieval
to validate Phase 5 hybrid retrieval improvements.

Usage:
    from ace.retrieval_analytics import RetrievalTracker, MetricsCalculator

    tracker = RetrievalTracker()
    tracker.log_retrieval(session_id, query, lessons, method="hybrid")

    calculator = MetricsCalculator(tracker)
    metrics = calculator.compute_all_metrics()
"""

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np


@dataclass
class RetrievalEvent:
    """Single retrieval event with lessons and scores."""

    session_id: str
    timestamp: float
    query: str
    method: Literal["keyword", "hybrid"]
    lessons: list[dict[str, Any]]  # [{id, score, matched_by}]
    top_n: int
    token_count: int | None = None


@dataclass
class FeedbackEvent:
    """Effectiveness feedback on a lesson."""

    session_id: str
    timestamp: float
    lesson_id: str
    helpful: bool  # True = helpful, False = harmful


class RetrievalTracker:
    """Track lesson retrieval decisions and effectiveness feedback.

    Stores events to JSON for later analysis.
    """

    def __init__(self, storage_path: Path | None = None):
        """Initialize tracker with storage path.

        Args:
            storage_path: Path to store tracking data. Defaults to
                         ~/.local/share/gptme/retrieval_analytics.json
        """
        if storage_path is None:
            storage_path = Path.home() / ".local/share/gptme/retrieval_analytics.json"
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing data
        self._data: dict[str, list[dict[str, Any]]] = {"retrievals": [], "feedback": []}
        if self.storage_path.exists():
            # Only load if file has content
            if self.storage_path.stat().st_size > 0:
                with open(self.storage_path) as f:
                    self._data = json.load(f)
            # else: keep default empty structure

    def log_retrieval(
        self,
        session_id: str,
        query: str,
        lessons: list[dict[str, Any]],
        method: Literal["keyword", "hybrid"],
        top_n: int = 5,
        token_count: int | None = None,
    ) -> None:
        """Log a retrieval event.

        Args:
            session_id: Unique session identifier
            query: User message that triggered retrieval
            lessons: Retrieved lessons with scores and metadata
            method: Retrieval method used (keyword or hybrid)
            top_n: Number of lessons included in context
            token_count: Total tokens in included lessons (if available)
        """
        event = RetrievalEvent(
            session_id=session_id,
            timestamp=time.time(),
            query=query,
            method=method,
            lessons=lessons,
            top_n=top_n,
            token_count=token_count,
        )
        self._data["retrievals"].append(asdict(event))
        self._save()

    def log_feedback(self, session_id: str, lesson_id: str, helpful: bool) -> None:
        """Log effectiveness feedback on a lesson.

        Args:
            session_id: Session where lesson was used
            lesson_id: Lesson identifier
            helpful: True if helpful, False if harmful
        """
        event = FeedbackEvent(
            session_id=session_id,
            timestamp=time.time(),
            lesson_id=lesson_id,
            helpful=helpful,
        )
        self._data["feedback"].append(asdict(event))
        self._save()

    def get_retrievals(
        self,
        session_id: str | None = None,
        method: Literal["keyword", "hybrid"] | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Get retrieval events with optional filtering.

        Args:
            session_id: Filter by session ID
            method: Filter by retrieval method
            since: Filter events after this timestamp

        Returns:
            List of retrieval events
        """
        results = self._data["retrievals"]

        if session_id:
            results = [r for r in results if r["session_id"] == session_id]
        if method:
            results = [r for r in results if r["method"] == method]
        if since:
            results = [r for r in results if r["timestamp"] >= since]

        return results

    def get_feedback(
        self, session_id: str | None = None, lesson_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get feedback events with optional filtering.

        Args:
            session_id: Filter by session ID
            lesson_id: Filter by lesson ID

        Returns:
            List of feedback events
        """
        results = self._data["feedback"]

        if session_id:
            results = [r for r in results if r["session_id"] == session_id]
        if lesson_id:
            results = [r for r in results if r["lesson_id"] == lesson_id]

        return results

    def _save(self) -> None:
        """Persist tracking data to storage."""
        with open(self.storage_path, "w") as f:
            json.dump(self._data, f, indent=2)


class MetricsCalculator:
    """Calculate analytics metrics from tracked retrieval data."""

    def __init__(self, tracker: RetrievalTracker):
        """Initialize with a retrieval tracker.

        Args:
            tracker: RetrievalTracker with historical data
        """
        self.tracker = tracker

    def compute_precision(
        self, method: Literal["keyword", "hybrid"] | None = None
    ) -> float:
        """Compute precision: helpful lessons / total included.

        Args:
            method: Filter by retrieval method

        Returns:
            Precision score (0.0-1.0)
        """
        retrievals = self.tracker.get_retrievals(method=method)
        if not retrievals:
            return 0.0

        # Count included lessons
        total_included = sum(len(r["lessons"][: r["top_n"]]) for r in retrievals)
        if total_included == 0:
            return 0.0

        # Count helpful feedback
        helpful_count = 0
        for retrieval in retrievals:
            session_id = retrieval["session_id"]
            top_lessons = retrieval["lessons"][: retrieval["top_n"]]

            for lesson in top_lessons:
                lesson_id = lesson.get("id")
                if not lesson_id:
                    continue

                feedback = self.tracker.get_feedback(
                    session_id=session_id, lesson_id=lesson_id
                )
                helpful_count += sum(1 for f in feedback if f["helpful"])

        return helpful_count / total_included if total_included > 0 else 0.0

    def compute_token_efficiency(
        self, method: Literal["keyword", "hybrid"] | None = None
    ) -> dict[str, float]:
        """Compute token efficiency metrics.

        Args:
            method: Filter by retrieval method

        Returns:
            Dictionary with:
            - avg_tokens: Average tokens per session
            - total_tokens: Total tokens across all sessions
            - sessions: Number of sessions
        """
        retrievals = self.tracker.get_retrievals(method=method)
        if not retrievals:
            return {"avg_tokens": 0.0, "total_tokens": 0, "sessions": 0}

        # Filter retrievals with token counts
        with_tokens = [r for r in retrievals if r.get("token_count") is not None]
        if not with_tokens:
            return {"avg_tokens": 0.0, "total_tokens": 0, "sessions": 0}

        total_tokens = sum(r["token_count"] for r in with_tokens)
        return {
            "avg_tokens": total_tokens / len(with_tokens),
            "total_tokens": total_tokens,
            "sessions": len(with_tokens),
        }

    def compute_effectiveness_distribution(
        self, method: Literal["keyword", "hybrid"] | None = None
    ) -> dict[str, int]:
        """Compute distribution of helpful/harmful/no-feedback.

        Args:
            method: Filter by retrieval method

        Returns:
            Dictionary with counts:
            - helpful: Lessons marked helpful
            - harmful: Lessons marked harmful
            - no_feedback: Lessons without feedback
        """
        retrievals = self.tracker.get_retrievals(method=method)
        counts = {"helpful": 0, "harmful": 0, "no_feedback": 0}

        for retrieval in retrievals:
            session_id = retrieval["session_id"]
            top_lessons = retrieval["lessons"][: retrieval["top_n"]]

            for lesson in top_lessons:
                lesson_id = lesson.get("id")
                if not lesson_id:
                    counts["no_feedback"] += 1
                    continue

                feedback = self.tracker.get_feedback(
                    session_id=session_id, lesson_id=lesson_id
                )

                if not feedback:
                    counts["no_feedback"] += 1
                elif any(f["helpful"] for f in feedback):
                    counts["helpful"] += 1
                else:
                    counts["harmful"] += 1

        return counts

    def compute_all_metrics(self) -> dict[str, dict[str, Any]]:
        """Compute all metrics for both keyword and hybrid methods.

        Returns:
            Dictionary with metrics for each method
        """
        metrics = {}
        for method in ["keyword", "hybrid"]:
            metrics[method] = {
                "precision": self.compute_precision(method=method),  # type: ignore
                "token_efficiency": self.compute_token_efficiency(method=method),  # type: ignore
                "effectiveness": self.compute_effectiveness_distribution(method=method),  # type: ignore
            }
        return metrics


class ABTestHarness:
    """Compare keyword vs hybrid retrieval on the same queries."""

    def __init__(self, tracker: RetrievalTracker):
        """Initialize with a retrieval tracker.

        Args:
            tracker: RetrievalTracker to log results
        """
        self.tracker = tracker

    def run_comparison(
        self,
        session_id: str,
        query: str,
        lessons_keyword: list[dict[str, Any]],
        lessons_hybrid: list[dict[str, Any]],
        top_n: int = 5,
    ) -> dict[str, Any]:
        """Run A/B comparison for a single query.

        Args:
            session_id: Session identifier
            query: User message
            lessons_keyword: Results from keyword-only retrieval
            lessons_hybrid: Results from hybrid retrieval
            top_n: Number of lessons to compare

        Returns:
            Comparison results with differences
        """
        # Log both retrievals
        self.tracker.log_retrieval(
            session_id=f"{session_id}_keyword",
            query=query,
            lessons=lessons_keyword,
            method="keyword",
            top_n=top_n,
        )

        self.tracker.log_retrieval(
            session_id=f"{session_id}_hybrid",
            query=query,
            lessons=lessons_hybrid,
            method="hybrid",
            top_n=top_n,
        )

        # Compare top-N
        keyword_top = set(lesson.get("id") for lesson in lessons_keyword[:top_n])
        hybrid_top = set(lesson.get("id") for lesson in lessons_hybrid[:top_n])

        return {
            "query": query,
            "overlap": len(keyword_top & hybrid_top),
            "keyword_only": list(keyword_top - hybrid_top),
            "hybrid_only": list(hybrid_top - keyword_top),
            "keyword_scores": [
                lesson.get("score", 0.0) for lesson in lessons_keyword[:top_n]
            ],
            "hybrid_scores": [
                lesson.get("score", 0.0) for lesson in lessons_hybrid[:top_n]
            ],
        }

    def aggregate_results(self, comparisons: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate multiple A/B test comparisons.

        Args:
            comparisons: List of comparison results from run_comparison()

        Returns:
            Aggregated statistics
        """
        if not comparisons:
            return {}

        overlaps = [c["overlap"] for c in comparisons]
        keyword_scores = [score for c in comparisons for score in c["keyword_scores"]]
        hybrid_scores = [score for c in comparisons for score in c["hybrid_scores"]]

        return {
            "num_comparisons": len(comparisons),
            "avg_overlap": np.mean(overlaps),
            "avg_keyword_score": np.mean(keyword_scores),
            "avg_hybrid_score": np.mean(hybrid_scores),
            "score_improvement": (
                np.mean(hybrid_scores) - np.mean(keyword_scores)
                if keyword_scores and hybrid_scores
                else 0.0
            ),
        }


class AnalyticsDashboard:
    """CLI dashboard to visualize retrieval analytics."""

    def __init__(self, calculator: MetricsCalculator):
        """Initialize with a metrics calculator.

        Args:
            calculator: MetricsCalculator with historical data
        """
        self.calculator = calculator

    def show_session_stats(self) -> str:
        """Show per-session statistics.

        Returns:
            Formatted statistics string
        """
        all_retrievals = self.calculator.tracker.get_retrievals()
        sessions = defaultdict(list)

        for r in all_retrievals:
            sessions[r["session_id"]].append(r)

        lines = ["# Session Statistics\n"]
        lines.append(f"Total sessions: {len(sessions)}\n")

        for session_id, retrievals in sorted(sessions.items()):
            method = retrievals[0]["method"]
            total_lessons = sum(len(r["lessons"]) for r in retrievals)
            lines.append(f"\n## {session_id} ({method})")
            lines.append(f"  Retrievals: {len(retrievals)}")
            lines.append(f"  Total lessons: {total_lessons}")

        return "\n".join(lines)

    def show_method_comparison(self) -> str:
        """Show comparison between keyword and hybrid methods.

        Returns:
            Formatted comparison string
        """
        metrics = self.calculator.compute_all_metrics()

        lines = ["# Method Comparison\n"]

        for method in ["keyword", "hybrid"]:
            m = metrics[method]
            lines.append(f"\n## {method.upper()}")
            lines.append(f"Precision: {m['precision']:.3f}")
            lines.append(f"Avg tokens: {m['token_efficiency']['avg_tokens']:.1f}")
            lines.append(
                f"Effectiveness: {m['effectiveness']['helpful']} helpful, "
                f"{m['effectiveness']['harmful']} harmful, "
                f"{m['effectiveness']['no_feedback']} no feedback"
            )

        return "\n".join(lines)

    def show_effectiveness_trends(self) -> str:
        """Show effectiveness trends over time.

        Returns:
            Formatted trends string
        """
        # Get all feedback sorted by time
        all_feedback = sorted(
            self.calculator.tracker.get_feedback(), key=lambda f: f["timestamp"]
        )

        if not all_feedback:
            return "# Effectiveness Trends\n\nNo feedback data available."

        # Group by day
        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"helpful": 0, "harmful": 0}
        )
        for f in all_feedback:
            day = datetime.fromtimestamp(f["timestamp"]).strftime("%Y-%m-%d")
            if f["helpful"]:
                daily[day]["helpful"] += 1
            else:
                daily[day]["harmful"] += 1

        lines = ["# Effectiveness Trends\n"]
        for day in sorted(daily.keys()):
            counts = daily[day]
            total = counts["helpful"] + counts["harmful"]
            precision = counts["helpful"] / total if total > 0 else 0.0
            lines.append(
                f"{day}: {counts['helpful']}/{total} helpful ({precision:.1%})"
            )

        return "\n".join(lines)
