"""
Pareto optimization utilities for lesson variant selection.

Implements Pareto dominance checking and front computation for multi-objective
optimization of lesson quality.
"""

from typing import Any, Dict, List, Tuple


def dominates(scores_a: Dict[str, float], scores_b: Dict[str, float]) -> bool:
    """Check if scores_a dominates scores_b.

    A dominates B if:
    - A is >= B on all dimensions
    - A is > B on at least one dimension

    Args:
        scores_a: Score dict for variant A
        scores_b: Score dict for variant B

    Returns:
        True if A dominates B, False otherwise
    """
    all_geq = all(scores_a.get(k, 0) >= scores_b.get(k, 0) for k in scores_b)
    any_gt = any(scores_a.get(k, 0) > scores_b.get(k, 0) for k in scores_b)

    return all_geq and any_gt


def compute_pareto_front(
    variants: List[Tuple[str, Dict[str, Any]]],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Compute Pareto front from list of (lesson, judge_result) tuples.

    Returns only non-dominated variants (those not dominated by any other variant).

    Args:
        variants: List of (lesson_markdown, judge_result) tuples

    Returns:
        List of non-dominated (lesson, judge_result) tuples
    """
    pareto_front = []

    for i, (lesson_i, result_i) in enumerate(variants):
        scores_i = result_i["scores"]
        is_dominated = False

        # Check if this variant is dominated by any other
        for j, (_, result_j) in enumerate(variants):
            if i == j:
                continue
            scores_j = result_j["scores"]

            if dominates(scores_j, scores_i):
                is_dominated = True
                break

        if not is_dominated:
            pareto_front.append((lesson_i, result_i))

    return pareto_front


def select_recommended_variant(
    pareto_front: List[Tuple[str, Dict[str, Any]]],
) -> Tuple[str, Dict[str, Any]]:
    """Select single recommended variant from Pareto front.

    Uses weighted average of scores, prioritizing:
    - detectability (2x weight) - most important for automation
    - enforceability (2x weight) - ensures actionability
    - correctness (1.5x weight) - must be right
    - others (1x weight)

    Args:
        pareto_front: List of (lesson, judge_result) tuples on Pareto front

    Returns:
        Single (lesson, judge_result) tuple with highest weighted score
    """
    if not pareto_front:
        raise ValueError("Pareto front is empty")

    if len(pareto_front) == 1:
        return pareto_front[0]

    # Define weights for each dimension
    weights = {
        "correctness": 1.5,
        "specificity": 1.0,
        "detectability": 2.0,  # Highest priority - enables automation
        "enforceability": 2.0,  # Highest priority - ensures actionability
        "brevity": 1.0,
        "evidence_use": 1.0,
    }

    best_variant = None
    best_score = -1.0

    for lesson, result in pareto_front:
        scores = result["scores"]

        # Compute weighted average
        weighted_sum = sum(
            scores.get(dim, 0) * weight for dim, weight in weights.items()
        )
        total_weight = sum(weights.values())
        weighted_avg = weighted_sum / total_weight

        if weighted_avg > best_score:
            best_score = weighted_avg
            best_variant = (lesson, result)

    if best_variant is None:
        # Fallback to first variant
        return pareto_front[0]

    return best_variant
