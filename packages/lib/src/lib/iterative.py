#!/usr/bin/env python3
"""
Iterative Analysis Framework

Provides multi-pass analysis for improved issue detection.
Based on insights from steveyegge/vc where iterative refinement
catches 15-30% more issues through multiple AI passes.

Key Concepts:
- Multiple analysis passes to catch what single-pass misses
- Convergence detection to stop when diminishing returns
- Result merging and deduplication
- Configurable iteration limits

Usage:
    from lib.iterative import IterativeAnalyzer, AnalysisResult

    class TaskDecomposer(IterativeAnalyzer):
        def analyze_single_pass(self, input_data, previous_results):
            # Your analysis logic here
            return new_findings

    analyzer = TaskDecomposer(min_iterations=2, max_iterations=5)
    results = analyzer.analyze(task_description)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

# Type variable for analysis results
T = TypeVar("T")


@dataclass
class IterationStats:
    """Statistics for a single iteration."""

    iteration: int
    new_findings: int
    total_findings: int
    converged: bool = False


@dataclass
class AnalysisResult(Generic[T]):
    """Complete analysis result with all iterations."""

    findings: list[T]
    iterations: int
    stats: list[IterationStats] = field(default_factory=list)
    converged: bool = False
    convergence_ratio: float = 0.0

    @property
    def total_findings(self) -> int:
        return len(self.findings)


class IterativeAnalyzer(ABC, Generic[T]):
    """
    Abstract base class for iterative analysis.

    Subclass and implement analyze_single_pass() for your specific use case.
    """

    def __init__(
        self,
        min_iterations: int = 2,
        max_iterations: int = 5,
        convergence_threshold: float = 0.15,
        verbose: bool = False,
    ):
        """
        Initialize iterative analyzer.

        Args:
            min_iterations: Minimum passes to run (ensures thoroughness)
            max_iterations: Maximum passes (prevents infinite loops)
            convergence_threshold: Stop if new findings ratio < this (0.15 = 15%)
            verbose: Enable debug output
        """
        self.min_iterations = min_iterations
        self.max_iterations = max_iterations
        self.convergence_threshold = convergence_threshold
        self.verbose = verbose

    @abstractmethod
    def analyze_single_pass(
        self, input_data: Any, previous_results: list[T], iteration: int
    ) -> list[T]:
        """
        Perform a single analysis pass.

        Args:
            input_data: The data to analyze
            previous_results: Results from all previous iterations
            iteration: Current iteration number (1-indexed)

        Returns:
            List of new findings from this pass
        """
        pass

    def deduplicate(self, findings: list[T]) -> list[T]:
        """
        Remove duplicate findings. Override for custom deduplication logic.

        Default implementation uses set() which requires hashable items.
        """
        # Try set-based dedup first
        try:
            seen: set[Any] = set()
            unique: list[T] = []
            for item in findings:
                if item not in seen:
                    seen.add(item)
                    unique.append(item)
            return unique
        except TypeError:
            # Items not hashable, use equality comparison
            unique = []
            for item in findings:
                if item not in unique:
                    unique.append(item)
            return unique

    def should_continue(
        self, iteration: int, new_count: int, total_count: int
    ) -> tuple[bool, bool]:
        """
        Determine if analysis should continue.

        Args:
            iteration: Current iteration number
            new_count: Number of new findings this iteration
            total_count: Total findings so far

        Returns:
            Tuple of (should_continue, converged)
        """
        # Must complete minimum iterations
        if iteration < self.min_iterations:
            return True, False

        # Stop at maximum iterations
        if iteration >= self.max_iterations:
            return False, False

        # Check convergence (new findings ratio)
        if total_count > 0:
            new_ratio = new_count / total_count
            if new_ratio < self.convergence_threshold:
                return False, True

        # Continue if we found something new
        return new_count > 0, False

    def analyze(self, input_data: Any) -> AnalysisResult[T]:
        """
        Run iterative analysis.

        Args:
            input_data: Data to analyze

        Returns:
            AnalysisResult with all findings and statistics
        """
        all_findings: list[T] = []
        stats: list[IterationStats] = []
        converged = False

        if self.verbose:
            print(f"🔄 Starting iterative analysis (max {self.max_iterations} passes)")

        for iteration in range(1, self.max_iterations + 1):
            if self.verbose:
                print(f"\n📊 Pass {iteration}/{self.max_iterations}")

            # Run single pass
            new_findings = self.analyze_single_pass(input_data, all_findings, iteration)

            # Merge and deduplicate
            pre_dedup_count = len(all_findings)
            all_findings.extend(new_findings)
            all_findings = self.deduplicate(all_findings)
            post_dedup_count = len(all_findings)

            # Calculate actual new findings (after dedup)
            actual_new = post_dedup_count - pre_dedup_count

            # Record stats
            iter_stats = IterationStats(
                iteration=iteration,
                new_findings=actual_new,
                total_findings=post_dedup_count,
            )
            stats.append(iter_stats)

            if self.verbose:
                print(
                    f"   Found {len(new_findings)} items, {actual_new} new after dedup"
                )
                print(f"   Total: {post_dedup_count} findings")

            # Check if we should continue
            should_continue, converged = self.should_continue(
                iteration, actual_new, post_dedup_count
            )

            if converged:
                iter_stats.converged = True
                if self.verbose:
                    print(f"✅ Converged at iteration {iteration}")
                break

            if not should_continue:
                if self.verbose:
                    print(f"⏹️  Stopping at iteration {iteration}")
                break

        # Calculate final convergence ratio
        if len(stats) >= 2:
            last_new = stats[-1].new_findings
            total = stats[-1].total_findings
            convergence_ratio = last_new / total if total > 0 else 0.0
        else:
            convergence_ratio = 1.0

        return AnalysisResult(
            findings=all_findings,
            iterations=len(stats),
            stats=stats,
            converged=converged,
            convergence_ratio=convergence_ratio,
        )


# Example concrete implementation for task decomposition
class TaskDecomposer(IterativeAnalyzer[str]):
    """
    Multi-pass task decomposition.

    Each pass finds subtasks that previous passes might have missed.
    Useful for breaking down complex tasks into actionable items.
    """

    def __init__(
        self,
        analyze_func: Any | None = None,
        **kwargs: Any,
    ):
        """
        Initialize task decomposer.

        Args:
            analyze_func: Optional function(task, previous, iteration) -> list[str]
                         If not provided, uses placeholder that returns empty list
            **kwargs: Arguments passed to IterativeAnalyzer
        """
        super().__init__(**kwargs)
        self._analyze_func = analyze_func

    def analyze_single_pass(
        self, input_data: Any, previous_results: list[str], iteration: int
    ) -> list[str]:
        """
        Decompose task into subtasks.

        First pass: Initial breakdown
        Subsequent passes: Find subtasks of subtasks, edge cases, prerequisites
        """
        if self._analyze_func:
            result: list[str] = self._analyze_func(
                input_data, previous_results, iteration
            )
            return result

        # Placeholder - in real usage, this would call an LLM
        # Return empty to demonstrate convergence
        return []


# Convenience function for simple use cases
def iterative_analyze(
    analyze_func: Any,
    input_data: Any,
    min_iterations: int = 2,
    max_iterations: int = 5,
    convergence_threshold: float = 0.15,
    verbose: bool = False,
) -> AnalysisResult[Any]:
    """
    Convenience function for iterative analysis without subclassing.

    Args:
        analyze_func: Function(input_data, previous_results, iteration) -> list
        input_data: Data to analyze
        min_iterations: Minimum passes
        max_iterations: Maximum passes
        convergence_threshold: Convergence threshold
        verbose: Enable verbose output

    Returns:
        AnalysisResult with all findings

    Example:
        def find_issues(code, previous, iteration):
            # Your analysis logic
            return new_issues

        result = iterative_analyze(find_issues, source_code, verbose=True)
        print(f"Found {result.total_findings} issues in {result.iterations} passes")
    """

    class FuncAnalyzer(IterativeAnalyzer[Any]):
        def analyze_single_pass(
            self, data: Any, previous: list[Any], iteration: int
        ) -> list[Any]:
            result: list[Any] = analyze_func(data, previous, iteration)
            return result

    analyzer = FuncAnalyzer(
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        convergence_threshold=convergence_threshold,
        verbose=verbose,
    )
    return analyzer.analyze(input_data)


if __name__ == "__main__":
    # Demo with synthetic data
    print("🧪 Iterative Analysis Framework Demo\n")

    # Simulate an analysis that finds fewer items each pass
    call_count = 0

    def mock_analyzer(data: str, previous: list[str], iteration: int) -> list[str]:
        global call_count
        call_count += 1

        # Simulate diminishing returns
        if iteration == 1:
            return ["task1", "task2", "task3", "task4", "task5"]
        elif iteration == 2:
            return ["task6", "task7", "task1"]  # task1 is duplicate
        elif iteration == 3:
            return ["task8", "task2"]  # task2 is duplicate
        else:
            return []  # Converged

    result = iterative_analyze(
        mock_analyzer,
        "Build a web application",
        min_iterations=2,
        max_iterations=5,
        convergence_threshold=0.15,
        verbose=True,
    )

    print("\n📈 Final Results:")
    print(f"   Total findings: {result.total_findings}")
    print(f"   Iterations: {result.iterations}")
    print(f"   Converged: {result.converged}")
    print(f"   Findings: {result.findings}")
