"""
GEPA-lite evolution loop for lesson generation.

Implements the core evolution mechanism:
1. Generate multiple lesson variants (exploration)
2. Judge each variant on multiple dimensions
3. Compute Pareto front (keep diverse high-quality variants)
4. Select recommended variant (best overall)
"""

from typing import Any, Dict, List, Tuple

from gptme_lessons_extras.utils.llm import llm_judge_score
from gptme_lessons_extras.utils.pareto import (
    compute_pareto_front,
    select_recommended_variant,
)
from gptme_lessons_extras.utils.variants import generate_lesson_variants


def gepa_lite_evolve(
    moment: Dict,
    conversation_id: str,
    num_variants: int = 5,
    temperature: float = 0.7,
    verbose: bool = False,
) -> Tuple[str, Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Run GEPA-lite evolution loop to generate optimized lesson.

    Process:
    1. Generate N lesson variants using different prompt strategies
    2. Judge each variant on 6 quality dimensions
    3. Compute Pareto front (non-dominated variants)
    4. Select recommended variant (best weighted score)

    Args:
        moment: Experience dict with title, context, evidence, etc.
        conversation_id: ID of source conversation
        num_variants: Number of variants to generate (default: 5)
        temperature: Base temperature for generation (default: 0.7)
        verbose: Print progress information (default: False)

    Returns:
        Tuple of:
        - recommended_lesson: Best lesson markdown
        - recommended_scores: Judge results for best lesson
        - pareto_front: List of (lesson, scores) tuples on Pareto front
    """
    if verbose:
        print("\n🧬 GEPA-lite Evolution Loop")
        print(f"  Generating {num_variants} variants...")

    # Step 1: Generate variants
    variants = generate_lesson_variants(
        moment=moment,
        conversation_id=conversation_id,
        num_variants=num_variants,
        temperature=temperature,
    )

    if verbose:
        print(f"  ✓ Generated {len(variants)} variants")
        print("\n  Judging variants...")

    # Step 2: Judge all variants
    judged_variants: List[Tuple[str, Dict[str, Any]]] = []

    for i, variant in enumerate(variants, 1):
        if verbose:
            print(f"    [{i}/{len(variants)}] Judging variant...")

        try:
            scores = llm_judge_score(
                lesson_markdown=variant,
                moment=moment,
                conversation_id=conversation_id,
                temperature=0.0,  # Deterministic judging
            )
        except Exception as e:
            if verbose:
                print(f"    ✗ Failed to judge variant: {e}")
            continue

        judged_variants.append((variant, scores))

        if verbose:
            avg_score = sum(scores["scores"].values()) / len(scores["scores"])
            print(f"    ✓ Average score: {avg_score:.3f}")

    if verbose:
        print("\n  Computing Pareto front...")

    # Step 3: Compute Pareto front
    pareto_front = compute_pareto_front(judged_variants)

    if verbose:
        print(f"  ✓ Pareto front size: {len(pareto_front)}/{len(variants)}")

    # Step 4: Select recommended variant
    if verbose:
        print("\n  Selecting recommended variant...")

    recommended_lesson, recommended_scores = select_recommended_variant(pareto_front)

    if verbose:
        print("  ✓ Selected recommended variant")
        print("\n📊 Score breakdown:")
        for dim, score in recommended_scores["scores"].items():
            print(f"    {dim:15s}: {score:.3f}")
        avg = sum(recommended_scores["scores"].values()) / len(
            recommended_scores["scores"]
        )
        print(f"    {'average':15s}: {avg:.3f}")

        print(f"\n💡 Rationale: {recommended_scores.get('rationale', 'N/A')}")

    return recommended_lesson, recommended_scores, pareto_front


def format_pareto_summary(pareto_front: List[Tuple[str, Dict[str, Any]]]) -> str:
    """Format Pareto front variants as human-readable summary.

    Args:
        pareto_front: List of (lesson, judge_result) tuples

    Returns:
        Formatted string describing the Pareto front
    """
    if not pareto_front:
        return "No variants on Pareto front"

    lines = [f"\n📊 Pareto Front ({len(pareto_front)} variants):\n"]

    for i, (_, result) in enumerate(pareto_front, 1):
        scores = result["scores"]
        avg_score = sum(scores.values()) / len(scores)

        # Find dominant dimensions (score > 0.7)
        strong_dims = [dim for dim, score in scores.items() if score > 0.7]

        lines.append(f"{i}. Average: {avg_score:.3f}")
        if strong_dims:
            lines.append(f"   Strong on: {', '.join(strong_dims)}")
        lines.append(
            f"   Scores: {', '.join(f'{k}={v:.2f}' for k, v in scores.items())}"
        )
        lines.append("")

    return "\n".join(lines)
