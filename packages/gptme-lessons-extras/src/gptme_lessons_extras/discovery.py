#!/usr/bin/env python3
"""
Lesson Discovery System - Component 3 of Phase 4.3 Phase 4

Implements intelligent lesson discovery through:
1. Recommendation engine: Suggest relevant lessons based on context
2. Similar lesson detection: Find duplicate/overlapping lessons

Created: 2025-10-29 (Session 370)
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Try importing sklearn, but provide fallback
try:
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
    from sklearn.metrics.pairwise import cosine_similarity  # type: ignore

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("Warning: scikit-learn not available. Using simpler similarity metrics.")


@dataclass
class LessonFeatures:
    """Extracted features from a lesson file."""

    lesson_id: str
    keywords: Set[str]
    category: str
    rule_text: str
    pattern_text: str
    full_text: str
    tools: Set[str]
    concepts: Set[str]


@dataclass
class RecommendationScore:
    """Score breakdown for a recommendation."""

    lesson_id: str
    total_score: float
    keyword_score: float = 0.0
    success_score: float = 0.0
    adoption_score: float = 0.0
    recency_score: float = 0.0


@dataclass
class SimilarityResult:
    """Similarity result between two lessons."""

    lesson_a: str
    lesson_b: str
    similarity_score: float
    keyword_overlap: float
    category_match: bool
    relationship: str  # exact, near-duplicate, complementary, redundant


class LessonDiscovery:
    """Main discovery system for lesson recommendations and similarity detection."""

    def __init__(
        self, lessons_dir: Path | None = None, history_dir: Path | None = None
    ):
        """Initialize discovery system.

        Args:
            lessons_dir: Path to lessons directory (default: lessons/)
            history_dir: Path to lesson history (default: .lessons-history/)
        """
        self.lessons_dir = lessons_dir or Path("lessons")
        self.history_dir = history_dir or Path(".lessons-history")
        self.features_cache: Dict[str, LessonFeatures] = {}

    def extract_features(self, lesson_path: Path) -> LessonFeatures:
        """Extract features from a lesson file."""
        lesson_id = lesson_path.stem

        # Check cache first
        if lesson_id in self.features_cache:
            return self.features_cache[lesson_id]

        content = lesson_path.read_text()

        # Extract frontmatter keywords
        keywords: Set[str] = set()
        frontmatter_match = re.search(r"---\n(.*?)\n---", content, re.DOTALL)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            # Look for keywords: [keyword1, keyword2] format
            keywords_match = re.search(r"keywords:\s*\[(.*?)\]", frontmatter)
            if keywords_match:
                keyword_list = keywords_match.group(1)
                keywords.update(k.strip().lower() for k in keyword_list.split(","))

        # Extract category from path
        category = lesson_path.parent.name

        # Extract rule text
        rule_match = re.search(r"## Rule\n(.*?)(?=\n##|$)", content, re.DOTALL)
        rule_text = rule_match.group(1).strip() if rule_match else ""

        # Extract pattern text
        pattern_match = re.search(r"## Pattern\n(.*?)(?=\n##|$)", content, re.DOTALL)
        pattern_text = pattern_match.group(1).strip() if pattern_match else ""

        # Extract tools (common tool names)
        tools = set()
        tool_patterns = ["shell", "git", "python", "tmux", "browser", "patch"]
        for tool in tool_patterns:
            if re.search(rf"\b{tool}\b", content, re.IGNORECASE):
                tools.add(tool)

        # Extract concepts (common concept words)
        concepts = set()
        concept_patterns = [
            "test",
            "debug",
            "commit",
            "pr",
            "issue",
            "ci",
            "autonomous",
        ]
        for concept in concept_patterns:
            if re.search(rf"\b{concept}\b", content, re.IGNORECASE):
                concepts.add(concept)

        features = LessonFeatures(
            lesson_id=lesson_id,
            keywords=keywords,
            category=category,
            rule_text=rule_text,
            pattern_text=pattern_text,
            full_text=content,
            tools=tools,
            concepts=concepts,
        )

        # Cache features
        self.features_cache[lesson_id] = features
        return features

    def load_metrics(self) -> Dict[str, Dict]:
        """Load aggregated metrics from Component 2."""
        metrics_file = self.history_dir / "metrics" / "network_metrics.json"
        if not metrics_file.exists():
            return {}

        try:
            with open(metrics_file) as f:
                data = json.load(f)
                return dict(data.get("lessons", {}))
        except (json.JSONDecodeError, KeyError):
            return {}

    def score_keyword_match(
        self, features: LessonFeatures, context_keywords: Set[str]
    ) -> float:
        """Score lesson based on keyword match with context.

        Returns score 0.0-1.0 based on Jaccard similarity.
        """
        if not context_keywords or not features.keywords:
            return 0.0

        # Combine lesson keywords with extracted tools and concepts
        lesson_terms = features.keywords | features.tools | features.concepts

        intersection = lesson_terms & context_keywords
        union = lesson_terms | context_keywords

        if not union:
            return 0.0

        return len(intersection) / len(union)

    def score_success_rate(self, lesson_id: str, metrics: Dict) -> float:
        """Score lesson based on success rate from metrics.

        Returns normalized score 0.0-1.0.
        """
        if lesson_id not in metrics:
            return 0.5  # Neutral score if no data

        lesson_metrics = metrics[lesson_id]
        success_rate = float(lesson_metrics.get("success_rate", 0.5))

        # Normalize to 0.0-1.0 (success_rate is already 0-1)
        return success_rate

    def score_adoption(self, lesson_id: str, metrics: Dict) -> float:
        """Score lesson based on adoption count.

        Returns normalized score 0.0-1.0.
        """
        if lesson_id not in metrics:
            return 0.0

        lesson_metrics = metrics[lesson_id]
        adoption_count = int(lesson_metrics.get("adoption_count", 0))

        # Normalize: 0 adoptions=0.0, 5+ adoptions=1.0
        return min(adoption_count / 5.0, 1.0)

    def recommend(
        self,
        context: str | None = None,
        keywords: List[str] | None = None,
        top_k: int = 5,
    ) -> List[RecommendationScore]:
        """Recommend lessons based on context.

        Args:
            context: Comma-separated context string (e.g., "shell,git,pr")
            keywords: List of keywords (alternative to context)
            top_k: Number of top recommendations to return

        Returns:
            List of RecommendationScore objects, sorted by total_score
        """
        # Parse context into keywords
        if context:
            context_keywords = set(context.lower().split(","))
        elif keywords:
            context_keywords = set(k.lower() for k in keywords)
        else:
            context_keywords = set()

        # Load metrics
        metrics = self.load_metrics()

        # Score all lessons
        scores = []
        for lesson_file in self.lessons_dir.rglob("*.md"):
            if lesson_file.name == "README.md":
                continue

            features = self.extract_features(lesson_file)
            lesson_id = features.lesson_id

            # Calculate component scores
            keyword_score = self.score_keyword_match(features, context_keywords)
            success_score = self.score_success_rate(lesson_id, metrics)
            adoption_score = self.score_adoption(lesson_id, metrics)
            recency_score = 0.0  # TODO: implement based on last access time

            # Weighted composite score
            total_score = (
                keyword_score * 0.4
                + success_score * 0.3
                + adoption_score * 0.2
                + recency_score * 0.1
            )

            scores.append(
                RecommendationScore(
                    lesson_id=lesson_id,
                    total_score=total_score,
                    keyword_score=keyword_score,
                    success_score=success_score,
                    adoption_score=adoption_score,
                    recency_score=recency_score,
                )
            )

        # Sort by total score and return top k
        scores.sort(key=lambda x: x.total_score, reverse=True)
        return scores[:top_k]

    def compute_text_similarity(self, text_a: str, text_b: str) -> float:
        """Compute text similarity using TF-IDF and cosine similarity.

        Falls back to simple word overlap if sklearn not available.
        """
        if SKLEARN_AVAILABLE:
            try:
                vectorizer = TfidfVectorizer()
                tfidf_matrix = vectorizer.fit_transform([text_a, text_b])
                similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
                return float(similarity[0][0])
            except Exception:
                # Fallback if TF-IDF fails
                pass

        # Simple fallback: word overlap
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())

        if not words_a or not words_b:
            return 0.0

        intersection = words_a & words_b
        union = words_a | words_b

        return len(intersection) / len(union) if union else 0.0

    def compute_keyword_overlap(
        self, features_a: LessonFeatures, features_b: LessonFeatures
    ) -> float:
        """Compute Jaccard similarity of keywords."""
        keywords_a = features_a.keywords | features_a.tools | features_a.concepts
        keywords_b = features_b.keywords | features_b.tools | features_b.concepts

        if not keywords_a or not keywords_b:
            return 0.0

        intersection = keywords_a & keywords_b
        union = keywords_a | keywords_b

        return len(intersection) / len(union) if union else 0.0

    def classify_relationship(
        self, similarity_score: float, keyword_overlap: float
    ) -> str:
        """Classify relationship between two lessons."""
        if similarity_score > 0.95 and keyword_overlap > 0.9:
            return "exact"
        elif similarity_score > 0.8 and keyword_overlap > 0.7:
            return "near-duplicate"
        elif 0.5 <= similarity_score <= 0.8:
            return "complementary"
        elif similarity_score > 0.6:
            return "redundant"
        else:
            return "unrelated"

    def find_similar(
        self, lesson_id: str, threshold: float = 0.5
    ) -> List[SimilarityResult]:
        """Find lessons similar to the given lesson.

        Args:
            lesson_id: ID of lesson to compare against
            threshold: Minimum similarity score (0.0-1.0)

        Returns:
            List of SimilarityResult objects, sorted by similarity
        """
        # Find target lesson
        target_path = None
        for lesson_file in self.lessons_dir.rglob("*.md"):
            if lesson_file.stem == lesson_id:
                target_path = lesson_file
                break

        if not target_path:
            raise ValueError(f"Lesson not found: {lesson_id}")

        target_features = self.extract_features(target_path)

        # Compare with all other lessons
        results = []
        for lesson_file in self.lessons_dir.rglob("*.md"):
            if lesson_file.name == "README.md" or lesson_file == target_path:
                continue

            compare_features = self.extract_features(lesson_file)

            # Compute similarity metrics
            text_similarity = self.compute_text_similarity(
                target_features.full_text, compare_features.full_text
            )

            keyword_overlap = self.compute_keyword_overlap(
                target_features, compare_features
            )

            # Skip if below threshold
            if text_similarity < threshold:
                continue

            category_match = target_features.category == compare_features.category
            relationship = self.classify_relationship(text_similarity, keyword_overlap)

            results.append(
                SimilarityResult(
                    lesson_a=lesson_id,
                    lesson_b=compare_features.lesson_id,
                    similarity_score=text_similarity,
                    keyword_overlap=keyword_overlap,
                    category_match=category_match,
                    relationship=relationship,
                )
            )

        # Sort by similarity score
        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results

    def find_all_duplicates(
        self, threshold: float = 0.8
    ) -> List[Tuple[str, str, float]]:
        """Find all potential duplicate lesson pairs.

        Args:
            threshold: Minimum similarity for duplicate detection

        Returns:
            List of (lesson_a, lesson_b, similarity_score) tuples
        """
        duplicates = []
        processed = set()

        lesson_files = [
            f for f in self.lessons_dir.rglob("*.md") if f.name != "README.md"
        ]

        for i, lesson_a in enumerate(lesson_files):
            features_a = self.extract_features(lesson_a)

            for lesson_b in lesson_files[i + 1 :]:
                features_b = self.extract_features(lesson_b)

                # Skip if already processed
                pair = tuple(sorted([features_a.lesson_id, features_b.lesson_id]))
                if pair in processed:
                    continue
                processed.add(pair)

                # Compute similarity
                text_similarity = self.compute_text_similarity(
                    features_a.full_text, features_b.full_text
                )

                if text_similarity >= threshold:
                    duplicates.append(
                        (features_a.lesson_id, features_b.lesson_id, text_similarity)
                    )

        # Sort by similarity score
        duplicates.sort(key=lambda x: x[2], reverse=True)
        return duplicates


def main():
    """CLI interface for lesson discovery."""
    import argparse

    parser = argparse.ArgumentParser(description="Lesson Discovery System")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Recommend command
    recommend_parser = subparsers.add_parser("recommend", help="Recommend lessons")
    recommend_parser.add_argument(
        "--context", type=str, help="Context keywords (comma-separated)"
    )
    recommend_parser.add_argument(
        "--keywords", nargs="+", help="Context keywords (space-separated)"
    )
    recommend_parser.add_argument(
        "--top-k", type=int, default=5, help="Number of recommendations"
    )

    # Similar command
    similar_parser = subparsers.add_parser("similar", help="Find similar lessons")
    similar_parser.add_argument("lesson_id", type=str, help="Lesson ID to compare")
    similar_parser.add_argument(
        "--threshold", type=float, default=0.5, help="Similarity threshold"
    )

    # Duplicates command
    duplicates_parser = subparsers.add_parser(
        "duplicates", help="Find duplicate lessons"
    )
    duplicates_parser.add_argument(
        "--threshold", type=float, default=0.8, help="Duplicate threshold"
    )

    args = parser.parse_args()

    # Initialize discovery system
    discovery = LessonDiscovery()

    if args.command == "recommend":
        # Get recommendations
        recommendations = discovery.recommend(
            context=args.context, keywords=args.keywords, top_k=args.top_k
        )

        print(f"\nTop {len(recommendations)} Recommended Lessons:\n")
        for i, rec in enumerate(recommendations, 1):
            print(f"{i}. {rec.lesson_id} (score: {rec.total_score:.2f})")
            print(f"   - Keyword match: {rec.keyword_score:.2f}")
            print(f"   - Success rate: {rec.success_score:.2f}")
            print(f"   - Adoption: {rec.adoption_score:.2f}")
            print()

    elif args.command == "similar":
        # Find similar lessons
        results = discovery.find_similar(args.lesson_id, args.threshold)

        print(f"\nLessons similar to {args.lesson_id}:\n")
        for result in results:
            print(f"- {result.lesson_b} (similarity: {result.similarity_score:.2f})")
            print(f"  Keyword overlap: {result.keyword_overlap:.2f}")
            print(f"  Category match: {result.category_match}")
            print(f"  Relationship: {result.relationship}")
            print()

    elif args.command == "duplicates":
        # Find all duplicates
        duplicates = discovery.find_all_duplicates(args.threshold)

        print(f"\nFound {len(duplicates)} potential duplicate pairs:\n")
        for lesson_a, lesson_b, similarity in duplicates:
            print(f"- {lesson_a} ≈ {lesson_b} (similarity: {similarity:.2f})")


if __name__ == "__main__":
    main()
