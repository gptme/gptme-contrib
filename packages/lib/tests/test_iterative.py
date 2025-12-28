#!/usr/bin/env python3
"""Tests for iterative analysis framework."""

import pytest

from lib.iterative import (
    AnalysisResult,
    TaskDecomposer,
    iterative_analyze,
)


class TestIterativeAnalyzer:
    """Test the base IterativeAnalyzer class."""

    def test_convergence_detection(self) -> None:
        """Test that analysis converges when new findings diminish."""
        call_count = 0

        def diminishing_returns(
            data: str, previous: list[str], iteration: int
        ) -> list[str]:
            nonlocal call_count
            call_count += 1
            # First pass finds many, subsequent passes find fewer
            if iteration == 1:
                return ["a", "b", "c", "d", "e"]
            elif iteration == 2:
                return ["f", "g"]  # 2 new
            elif iteration == 3:
                return ["h", "a"]  # 1 new (a is duplicate)
            else:
                return []  # None

        result = iterative_analyze(
            diminishing_returns,
            "test input",
            min_iterations=2,
            max_iterations=6,
            convergence_threshold=0.15,
            verbose=False,
        )

        assert result.total_findings == 8  # a-h
        assert result.iterations >= 2  # At least min_iterations
        assert result.iterations <= 6  # At most max_iterations

    def test_minimum_iterations_enforced(self) -> None:
        """Test that minimum iterations are always run."""
        call_count = 0

        def no_new_findings(
            data: str, previous: list[str], iteration: int
        ) -> list[str]:
            nonlocal call_count
            call_count += 1
            if iteration == 1:
                return ["only_first"]
            return []

        iterative_analyze(
            no_new_findings,
            "test",
            min_iterations=3,
            max_iterations=5,
            verbose=False,
        )

        assert call_count >= 3  # Min iterations enforced

    def test_maximum_iterations_respected(self) -> None:
        """Test that maximum iterations are not exceeded."""
        call_count = 0

        def always_finds_new(
            data: str, previous: list[str], iteration: int
        ) -> list[str]:
            nonlocal call_count
            call_count += 1
            return [f"item_{iteration}_{i}" for i in range(5)]

        result = iterative_analyze(
            always_finds_new,
            "test",
            min_iterations=1,
            max_iterations=3,
            verbose=False,
        )

        assert call_count == 3  # Max iterations respected
        assert result.iterations == 3

    def test_deduplication(self) -> None:
        """Test that duplicate findings are removed."""

        def with_duplicates(
            data: str, previous: list[str], iteration: int
        ) -> list[str]:
            if iteration == 1:
                return ["a", "b", "c"]
            elif iteration == 2:
                return ["a", "b", "d"]  # a, b are duplicates
            else:
                return ["a", "e"]  # a is duplicate

        result = iterative_analyze(
            with_duplicates,
            "test",
            min_iterations=3,
            max_iterations=3,
            verbose=False,
        )

        # Should have a, b, c, d, e (5 unique)
        assert result.total_findings == 5
        assert set(result.findings) == {"a", "b", "c", "d", "e"}

    def test_stats_tracking(self) -> None:
        """Test that iteration statistics are properly tracked."""

        def predictable_findings(
            data: str, previous: list[str], iteration: int
        ) -> list[str]:
            return [f"item_{iteration}_{i}" for i in range(iteration)]

        result = iterative_analyze(
            predictable_findings,
            "test",
            min_iterations=3,
            max_iterations=3,
            verbose=False,
        )

        assert len(result.stats) == 3
        # First iteration: 1 item (item_1_0)
        assert result.stats[0].new_findings == 1
        # Second iteration: 2 new items
        assert result.stats[1].new_findings == 2
        # Third iteration: 3 new items
        assert result.stats[2].new_findings == 3


class TestTaskDecomposer:
    """Test the TaskDecomposer concrete implementation."""

    def test_with_custom_function(self) -> None:
        """Test TaskDecomposer with custom analysis function."""

        def decompose(task: str, previous: list[str], iteration: int) -> list[str]:
            if iteration == 1:
                return ["Setup project", "Write code", "Test"]
            elif iteration == 2:
                return ["Configure CI", "Write code"]  # duplicate
            return []

        decomposer = TaskDecomposer(
            analyze_func=decompose,
            min_iterations=2,
            max_iterations=3,
        )

        result = decomposer.analyze("Build web app")

        assert result.total_findings == 4
        assert "Setup project" in result.findings
        assert "Configure CI" in result.findings

    def test_without_function_returns_empty(self) -> None:
        """Test TaskDecomposer without function returns empty results."""
        decomposer = TaskDecomposer(min_iterations=1, max_iterations=2)
        result = decomposer.analyze("Any task")

        assert result.total_findings == 0
        assert result.iterations >= 1


class TestAnalysisResult:
    """Test the AnalysisResult dataclass."""

    def test_total_findings_property(self) -> None:
        """Test total_findings property."""
        result: AnalysisResult[str] = AnalysisResult(
            findings=["a", "b", "c"],
            iterations=2,
            converged=True,
            convergence_ratio=0.1,
        )
        assert result.total_findings == 3

    def test_empty_result(self) -> None:
        """Test empty analysis result."""
        result: AnalysisResult[str] = AnalysisResult(
            findings=[],
            iterations=1,
            converged=False,
            convergence_ratio=1.0,
        )
        assert result.total_findings == 0
        assert not result.converged


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
