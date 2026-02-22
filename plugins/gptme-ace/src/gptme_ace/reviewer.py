#!/usr/bin/env python3
"""
ACE Reviewer: Review and evaluate pending deltas before approval.

Part of ACE Phase 5 utilities for lesson lifecycle management.

The Reviewer:
1. Loads pending deltas from deltas/pending/
2. Evaluates quality against criteria (relevance, clarity, non-duplication)
3. Provides recommendations: approve, reject, needs_revision
4. Supports both automatic LLM-based and manual review workflows

Usage:
    python -m gptme_ace.reviewer review --delta-id abc123
    python -m gptme_ace.reviewer batch --all
    python -m gptme_ace.reviewer status
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ReviewDecision(Enum):
    """Possible review decisions"""

    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_REVISION = "needs_revision"
    PENDING = "pending"


@dataclass
class ReviewCriterion:
    """A single review criterion with score and feedback"""

    name: str
    description: str
    score: float  # 0.0 to 1.0
    max_score: float = 1.0
    feedback: str = ""
    weight: float = 1.0


@dataclass
class ReviewResult:
    """Complete review result for a delta"""

    delta_id: str
    reviewed_at: str
    reviewer: str
    decision: ReviewDecision
    overall_score: float
    criteria: list[ReviewCriterion]
    summary: str
    suggestions: list[str] = field(default_factory=list)
    auto_approved: bool = False


class ReviewerError(Exception):
    """Error during review process"""

    pass


# Default review criteria
DEFAULT_CRITERIA = [
    ReviewCriterion(
        name="relevance",
        description="Is the delta relevant to the lesson's purpose and scope?",
        score=0.0,
        weight=1.5,
    ),
    ReviewCriterion(
        name="clarity",
        description="Is the content clear, concise, and well-structured?",
        score=0.0,
        weight=1.2,
    ),
    ReviewCriterion(
        name="actionability",
        description="Does the content provide actionable guidance?",
        score=0.0,
        weight=1.3,
    ),
    ReviewCriterion(
        name="non_duplication",
        description="Does this add new value without duplicating existing content?",
        score=0.0,
        weight=1.0,
    ),
    ReviewCriterion(
        name="accuracy",
        description="Is the content technically accurate and correct?",
        score=0.0,
        weight=1.5,
    ),
    ReviewCriterion(
        name="format_compliance",
        description="Does the content follow lesson format guidelines?",
        score=0.0,
        weight=0.8,
    ),
]


class DeltaReviewer:
    """Review and evaluate pending deltas"""

    def __init__(
        self,
        delta_dir: Path | None = None,
        lessons_dir: Path | None = None,
        approve_threshold: float = 0.7,
        reject_threshold: float = 0.4,
        auto_approve: bool = False,
    ):
        """
        Initialize the DeltaReviewer.

        Args:
            delta_dir: Directory containing delta files (default: ./deltas)
            lessons_dir: Directory containing lesson files for context (default: ./lessons)
            approve_threshold: Minimum score for auto-approval (0.0-1.0)
            reject_threshold: Score below which to auto-reject (0.0-1.0)
            auto_approve: If True, automatically approve/reject based on thresholds
        """
        self.delta_dir = delta_dir or Path("deltas")
        self.lessons_dir = lessons_dir or Path("lessons")
        self.approve_threshold = approve_threshold
        self.reject_threshold = reject_threshold
        self.auto_approve = auto_approve

        # Ensure review output directory exists
        self.reviews_dir = self.delta_dir / "reviews"
        self.reviews_dir.mkdir(exist_ok=True, parents=True)

    def load_pending_delta(self, delta_id: str) -> dict:
        """Load a delta from pending directory"""
        delta_path = self.delta_dir / "pending" / f"{delta_id}.json"
        if not delta_path.exists():
            # Check if already reviewed/applied
            approved_path = self.delta_dir / "approved" / f"{delta_id}.json"
            if approved_path.exists():
                raise ReviewerError(f"Delta {delta_id} is already approved")
            applied_path = self.delta_dir / "applied" / f"{delta_id}.json"
            if applied_path.exists():
                raise ReviewerError(f"Delta {delta_id} has already been applied")
            raise ReviewerError(f"Delta {delta_id} not found in pending/")

        return json.loads(delta_path.read_text())

    def list_pending_deltas(self) -> list[str]:
        """List all pending delta IDs"""
        pending_dir = self.delta_dir / "pending"
        if not pending_dir.exists():
            return []
        return [p.stem for p in pending_dir.glob("*.json")]

    def load_lesson_context(self, lesson_id: str) -> str | None:
        """Load the existing lesson content for context"""
        # Try direct path first
        lesson_path = self.lessons_dir / f"{lesson_id}.md"
        if lesson_path.exists():
            return lesson_path.read_text()

        # Try searching by filename
        name = lesson_id.split("/")[-1] if "/" in lesson_id else lesson_id
        for path in self.lessons_dir.rglob(f"{name}.md"):
            return path.read_text()

        return None

    def evaluate_criterion(
        self,
        criterion: ReviewCriterion,
        delta: dict,
        lesson_content: str | None,
    ) -> ReviewCriterion:
        """
        Evaluate a single criterion for a delta.

        This is a heuristic-based evaluation. For more sophisticated
        evaluation, use evaluate_with_llm().

        Args:
            criterion: The criterion to evaluate
            delta: The delta dict
            lesson_content: Existing lesson content for context

        Returns:
            Updated criterion with score and feedback
        """
        # Create a copy to avoid mutating the template
        result = ReviewCriterion(
            name=criterion.name,
            description=criterion.description,
            score=0.5,  # Default middle score
            max_score=criterion.max_score,
            weight=criterion.weight,
        )

        operations = delta.get("operations", [])
        rationale = delta.get("rationale", "")

        if criterion.name == "relevance":
            # Check if rationale explains relevance
            if len(rationale) > 50:
                result.score = 0.7
                result.feedback = "Rationale provided"
            else:
                result.score = 0.4
                result.feedback = "Limited rationale"

        elif criterion.name == "clarity":
            # Check operation content clarity
            total_content_len = sum(len(op.get("content", "")) for op in operations)
            if total_content_len > 100:
                result.score = 0.6
                result.feedback = "Sufficient content provided"
            else:
                result.score = 0.5
                result.feedback = "Content length acceptable"

        elif criterion.name == "actionability":
            # Check for actionable language in operations
            action_words = ["use", "always", "never", "do", "avoid", "ensure"]
            content_text = " ".join(op.get("content", "").lower() for op in operations)
            action_count = sum(1 for word in action_words if word in content_text)
            if action_count >= 3:
                result.score = 0.8
                result.feedback = "Contains actionable guidance"
            elif action_count >= 1:
                result.score = 0.6
                result.feedback = "Some actionable content"
            else:
                result.score = 0.4
                result.feedback = "May lack actionable guidance"

        elif criterion.name == "non_duplication":
            # Basic duplication check against existing lesson
            if lesson_content:
                # Simple check: look for significant overlap
                content_text = " ".join(op.get("content", "") for op in operations)
                # Very basic overlap check
                if content_text and content_text[:50] in lesson_content:
                    result.score = 0.3
                    result.feedback = "Possible duplication detected"
                else:
                    result.score = 0.7
                    result.feedback = "No obvious duplication"
            else:
                result.score = 0.8
                result.feedback = "New lesson, no duplication possible"

        elif criterion.name == "accuracy":
            # Heuristic: assume accuracy if well-formatted
            if operations and all(op.get("type") for op in operations):
                result.score = 0.6
                result.feedback = "Well-structured operations"
            else:
                result.score = 0.4
                result.feedback = "May need accuracy verification"

        elif criterion.name == "format_compliance":
            # Check operation structure
            valid_ops = all(
                op.get("type") in ["ADD", "REMOVE", "MODIFY"] for op in operations
            )
            if valid_ops and all(op.get("section") for op in operations):
                result.score = 0.9
                result.feedback = "Operations follow format guidelines"
            elif valid_ops:
                result.score = 0.6
                result.feedback = "Valid operations, sections could be clearer"
            else:
                result.score = 0.3
                result.feedback = "Invalid operation types or structure"

        return result

    def review_delta(
        self,
        delta_id: str,
        reviewer: str = "ace_reviewer",
        criteria: list[ReviewCriterion] | None = None,
    ) -> ReviewResult:
        """
        Review a single delta and return the result.

        Args:
            delta_id: ID of the delta to review
            reviewer: Name of the reviewer (human or system)
            criteria: Custom criteria (defaults to DEFAULT_CRITERIA)

        Returns:
            ReviewResult with scores and decision
        """
        delta = self.load_pending_delta(delta_id)
        lesson_id = delta.get("lesson_id", "")
        lesson_content = self.load_lesson_context(lesson_id)

        # Use default criteria if not provided
        if criteria is None:
            criteria = [
                ReviewCriterion(
                    name=c.name,
                    description=c.description,
                    score=0.0,
                    max_score=c.max_score,
                    weight=c.weight,
                )
                for c in DEFAULT_CRITERIA
            ]

        # Evaluate each criterion
        evaluated_criteria = []
        for criterion in criteria:
            result = self.evaluate_criterion(criterion, delta, lesson_content)
            evaluated_criteria.append(result)

        # Calculate weighted overall score
        total_weight = sum(c.weight for c in evaluated_criteria)
        weighted_sum = sum(c.score * c.weight for c in evaluated_criteria)
        overall_score = weighted_sum / total_weight if total_weight > 0 else 0.0

        # Determine decision based on score and thresholds
        if overall_score >= self.approve_threshold:
            decision = ReviewDecision.APPROVE
        elif overall_score < self.reject_threshold:
            decision = ReviewDecision.REJECT
        else:
            decision = ReviewDecision.NEEDS_REVISION

        # Generate suggestions
        suggestions = []
        for c in evaluated_criteria:
            if c.score < 0.5:
                suggestions.append(f"Improve {c.name}: {c.feedback}")

        # Generate summary
        summary = self._generate_summary(
            delta, evaluated_criteria, overall_score, decision
        )

        result = ReviewResult(
            delta_id=delta_id,
            reviewed_at=datetime.now(timezone.utc).isoformat(),
            reviewer=reviewer,
            decision=decision,
            overall_score=round(overall_score, 3),
            criteria=evaluated_criteria,
            summary=summary,
            suggestions=suggestions,
            auto_approved=self.auto_approve and decision == ReviewDecision.APPROVE,
        )

        return result

    def _generate_summary(
        self,
        delta: dict,
        criteria: list[ReviewCriterion],
        overall_score: float,
        decision: ReviewDecision,
    ) -> str:
        """Generate a human-readable summary of the review"""
        lesson_id = delta.get("lesson_id", "unknown")
        op_count = len(delta.get("operations", []))

        high_scores = [c for c in criteria if c.score >= 0.7]
        low_scores = [c for c in criteria if c.score < 0.5]

        summary_parts = [
            f"Review of delta for lesson '{lesson_id}' ({op_count} operations).",
            f"Overall score: {overall_score:.1%} - Decision: {decision.value.upper()}.",
        ]

        if high_scores:
            summary_parts.append(
                f"Strengths: {', '.join(c.name for c in high_scores)}."
            )

        if low_scores:
            summary_parts.append(
                f"Areas for improvement: {', '.join(c.name for c in low_scores)}."
            )

        return " ".join(summary_parts)

    def save_review(self, result: ReviewResult) -> Path:
        """Save review result to file"""
        review_path = self.reviews_dir / f"{result.delta_id}_review.json"

        review_dict = {
            "delta_id": result.delta_id,
            "reviewed_at": result.reviewed_at,
            "reviewer": result.reviewer,
            "decision": result.decision.value,
            "overall_score": result.overall_score,
            "criteria": [
                {
                    "name": c.name,
                    "description": c.description,
                    "score": c.score,
                    "max_score": c.max_score,
                    "feedback": c.feedback,
                    "weight": c.weight,
                }
                for c in result.criteria
            ],
            "summary": result.summary,
            "suggestions": result.suggestions,
            "auto_approved": result.auto_approved,
        }

        review_path.write_text(json.dumps(review_dict, indent=2))
        logger.info(f"Saved review to {review_path}")
        return review_path

    def move_to_approved(self, delta_id: str) -> Path:
        """Move a delta from pending to approved"""
        pending_path = self.delta_dir / "pending" / f"{delta_id}.json"
        approved_dir = self.delta_dir / "approved"
        approved_dir.mkdir(exist_ok=True)
        approved_path = approved_dir / f"{delta_id}.json"

        if not pending_path.exists():
            raise ReviewerError(f"Delta {delta_id} not found in pending/")

        # Load, update status, and save
        delta = json.loads(pending_path.read_text())
        delta["review_status"] = "approved"
        delta["approved_at"] = datetime.now(timezone.utc).isoformat()

        approved_path.write_text(json.dumps(delta, indent=2))
        pending_path.unlink()

        logger.info(f"Moved delta {delta_id} to approved/")
        return approved_path

    def move_to_rejected(self, delta_id: str, reason: str = "") -> Path:
        """Move a delta from pending to rejected"""
        pending_path = self.delta_dir / "pending" / f"{delta_id}.json"
        rejected_dir = self.delta_dir / "rejected"
        rejected_dir.mkdir(exist_ok=True)
        rejected_path = rejected_dir / f"{delta_id}.json"

        if not pending_path.exists():
            raise ReviewerError(f"Delta {delta_id} not found in pending/")

        # Load, update status, and save
        delta = json.loads(pending_path.read_text())
        delta["review_status"] = "rejected"
        delta["rejected_at"] = datetime.now(timezone.utc).isoformat()
        delta["rejection_reason"] = reason

        rejected_path.write_text(json.dumps(delta, indent=2))
        pending_path.unlink()

        logger.info(f"Moved delta {delta_id} to rejected/")
        return rejected_path

    def review_and_process(
        self,
        delta_id: str,
        reviewer_name: str = "ace_reviewer",
    ) -> ReviewResult:
        """
        Review a delta and optionally move it based on decision.

        If auto_approve is True, will automatically move approved/rejected
        deltas to their respective directories.

        Args:
            delta_id: ID of the delta to review
            reviewer_name: Name of the reviewer

        Returns:
            ReviewResult with decision
        """
        result = self.review_delta(delta_id, reviewer_name)
        self.save_review(result)

        if self.auto_approve:
            if result.decision == ReviewDecision.APPROVE:
                self.move_to_approved(delta_id)
                # Mark that automatic action was taken
                result.auto_approved = True
                logger.info(f"Auto-approved delta {delta_id}")
            elif result.decision == ReviewDecision.REJECT:
                self.move_to_rejected(delta_id, result.summary)
                # Mark that automatic action was taken (rejection is also an auto action)
                result.auto_approved = True
                logger.info(f"Auto-rejected delta {delta_id}")

        return result

    def batch_review(
        self,
        delta_ids: list[str] | None = None,
        reviewer_name: str = "ace_reviewer",
    ) -> list[ReviewResult]:
        """
        Review multiple deltas.

        Args:
            delta_ids: List of delta IDs (if None, requires explicit --all flag in CLI)
            reviewer_name: Name of the reviewer

        Returns:
            List of ReviewResults
        """
        if delta_ids is None:
            delta_ids = self.list_pending_deltas()

        results = []
        for delta_id in delta_ids:
            try:
                result = self.review_and_process(delta_id, reviewer_name)
                results.append(result)
            except ReviewerError as e:
                logger.warning(f"Failed to review {delta_id}: {e}")

        return results

    def get_status(self) -> dict:
        """Get status summary of deltas and reviews"""
        pending_count = len(self.list_pending_deltas())

        approved_dir = self.delta_dir / "approved"
        approved_count = (
            len(list(approved_dir.glob("*.json"))) if approved_dir.exists() else 0
        )

        rejected_dir = self.delta_dir / "rejected"
        rejected_count = (
            len(list(rejected_dir.glob("*.json"))) if rejected_dir.exists() else 0
        )

        applied_dir = self.delta_dir / "applied"
        applied_count = (
            len(list(applied_dir.glob("*.json"))) if applied_dir.exists() else 0
        )

        reviews_count = (
            len(list(self.reviews_dir.glob("*_review.json")))
            if self.reviews_dir.exists()
            else 0
        )

        return {
            "pending": pending_count,
            "approved": approved_count,
            "rejected": rejected_count,
            "applied": applied_count,
            "total_reviews": reviews_count,
        }


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="ACE Reviewer - Review and evaluate pending deltas"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Review command
    review_parser = subparsers.add_parser("review", help="Review a single delta")
    review_parser.add_argument("--delta-id", required=True, help="Delta ID to review")
    review_parser.add_argument(
        "--reviewer", default="ace_reviewer", help="Reviewer name"
    )
    review_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve/reject based on score",
    )

    # Batch command
    batch_parser = subparsers.add_parser("batch", help="Review all pending deltas")
    batch_parser.add_argument("--all", action="store_true", help="Review all pending")
    batch_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve/reject based on score",
    )
    batch_parser.add_argument(
        "--reviewer", default="ace_reviewer", help="Reviewer name"
    )

    # Status command
    subparsers.add_parser("status", help="Show delta status summary")

    # Approve command
    approve_parser = subparsers.add_parser("approve", help="Manually approve a delta")
    approve_parser.add_argument("--delta-id", required=True, help="Delta ID to approve")

    # Reject command
    reject_parser = subparsers.add_parser("reject", help="Manually reject a delta")
    reject_parser.add_argument("--delta-id", required=True, help="Delta ID to reject")
    reject_parser.add_argument("--reason", default="", help="Rejection reason")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    reviewer = DeltaReviewer(auto_approve=getattr(args, "auto_approve", False))

    if args.command == "review":
        result = reviewer.review_and_process(args.delta_id, reviewer_name=args.reviewer)
        print(f"\n{result.summary}")
        print(f"Decision: {result.decision.value.upper()}")
        print(f"Score: {result.overall_score:.1%}")
        if result.suggestions:
            print("\nSuggestions:")
            for s in result.suggestions:
                print(f"  - {s}")

    elif args.command == "batch":
        if not args.all:
            print(
                "Error: batch command requires --all flag to review all pending deltas"
            )
            print("This prevents accidental batch operations.")
            print("Use: python -m gptme_ace.reviewer batch --all")
            sys.exit(1)
        results = reviewer.batch_review(reviewer_name=args.reviewer)
        print(f"\nReviewed {len(results)} deltas:")
        for r in results:
            print(f"  {r.delta_id}: {r.decision.value} ({r.overall_score:.1%})")

    elif args.command == "status":
        status = reviewer.get_status()
        print("\nDelta Status:")
        print(f"  Pending:  {status['pending']}")
        print(f"  Approved: {status['approved']}")
        print(f"  Rejected: {status['rejected']}")
        print(f"  Applied:  {status['applied']}")
        print(f"  Reviews:  {status['total_reviews']}")

    elif args.command == "approve":
        reviewer.move_to_approved(args.delta_id)
        print(f"Approved delta {args.delta_id}")

    elif args.command == "reject":
        reviewer.move_to_rejected(args.delta_id, args.reason)
        print(f"Rejected delta {args.delta_id}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
