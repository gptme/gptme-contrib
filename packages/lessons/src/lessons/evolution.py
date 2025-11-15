#!/usr/bin/env python3
"""
Lesson Evolution Tracking for Agent Network

Phase 4.3 Phase 4 Component 1: Version history and contributor attribution.

Tracks:
- Version history across network
- Contributor attribution
- Refinement suggestions
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class LessonVersion:
    """Single version of a lesson."""

    version: int  # Incrementing version number
    timestamp: str  # ISO 8601 timestamp
    contributor: str  # Agent ID who made this change
    changes: str  # Description of changes made
    content_hash: str  # Hash of lesson content for verification

    @classmethod
    def from_dict(cls, data: dict) -> "LessonVersion":
        """Create version from dictionary."""
        return cls(**data)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "contributor": self.contributor,
            "changes": self.changes,
            "content_hash": self.content_hash,
        }


@dataclass
class LessonHistory:
    """Complete history of a lesson across the network."""

    lesson_id: str  # Lesson identifier (filename)
    origin_agent: str  # Agent who created the lesson
    created: str  # ISO 8601 timestamp of creation
    versions: list[LessonVersion] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "LessonHistory":
        """Create history from dictionary."""
        versions = [LessonVersion.from_dict(v) for v in data.get("versions", [])]
        return cls(
            lesson_id=data["lesson_id"],
            origin_agent=data["origin_agent"],
            created=data["created"],
            versions=versions,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "lesson_id": self.lesson_id,
            "origin_agent": self.origin_agent,
            "created": self.created,
            "versions": [v.to_dict() for v in self.versions],
        }

    def latest_version(self) -> Optional[LessonVersion]:
        """Get the most recent version."""
        return self.versions[-1] if self.versions else None

    def get_version(self, version_num: int) -> Optional[LessonVersion]:
        """Get specific version by number."""
        for v in self.versions:
            if v.version == version_num:
                return v
        return None

    def contributors(self) -> list[str]:
        """Get list of all contributors."""
        return list(set([self.origin_agent] + [v.contributor for v in self.versions]))


@dataclass
class RefinementSuggestion:
    """Proposed improvement to a lesson."""

    lesson_id: str  # Lesson to improve
    suggester: str  # Agent proposing refinement
    timestamp: str  # ISO 8601 timestamp
    category: str  # Type of refinement (clarity, examples, pattern, etc.)
    suggestion: str  # Description of proposed improvement
    priority: str  # high, medium, low
    status: str  # proposed, accepted, rejected, implemented

    @classmethod
    def from_dict(cls, data: dict) -> "RefinementSuggestion":
        """Create suggestion from dictionary."""
        return cls(**data)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "lesson_id": self.lesson_id,
            "suggester": self.suggester,
            "timestamp": self.timestamp,
            "category": self.category,
            "suggestion": self.suggestion,
            "priority": self.priority,
            "status": self.status,
        }


class EvolutionTracker:
    """Manages lesson evolution tracking."""

    def __init__(self, history_dir: Path = Path(".lessons-history")):
        """Initialize tracker with history directory."""
        self.history_dir = history_dir
        self.history_dir.mkdir(exist_ok=True)
        (self.history_dir / "refinements").mkdir(exist_ok=True)

    def _history_path(self, lesson_id: str) -> Path:
        """Get path to lesson history file."""
        return self.history_dir / f"{lesson_id}.json"

    def _refinements_path(self, lesson_id: str) -> Path:
        """Get path to refinements file."""
        return self.history_dir / "refinements" / f"{lesson_id}.json"

    def _compute_content_hash(self, content: str) -> str:
        """Compute hash of lesson content."""
        import hashlib

        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def initialize_lesson(
        self,
        lesson_id: str,
        origin_agent: str,
        content: str,
    ) -> LessonHistory:
        """Initialize tracking for a new lesson."""
        history = LessonHistory(
            lesson_id=lesson_id,
            origin_agent=origin_agent,
            created=datetime.now().isoformat(),
        )

        # Add initial version
        initial_version = LessonVersion(
            version=1,
            timestamp=history.created,
            contributor=origin_agent,
            changes="Initial creation",
            content_hash=self._compute_content_hash(content),
        )
        history.versions.append(initial_version)

        # Save to disk
        self._save_history(history)
        return history

    def track_change(
        self,
        lesson_id: str,
        contributor: str,
        changes: str,
        content: str,
    ) -> LessonVersion:
        """Record a new version of a lesson."""
        # Load existing history or create new
        history = self.load_history(lesson_id)
        if not history:
            raise ValueError(
                f"No history found for lesson {lesson_id}. Initialize first."
            )

        # Create new version
        latest = history.latest_version()
        if not latest:
            raise ValueError(f"No versions in history for {lesson_id}")
        new_version = LessonVersion(
            version=latest.version + 1,
            timestamp=datetime.now().isoformat(),
            contributor=contributor,
            changes=changes,
            content_hash=self._compute_content_hash(content),
        )

        history.versions.append(new_version)
        self._save_history(history)
        return new_version

    def load_history(self, lesson_id: str) -> Optional[LessonHistory]:
        """Load lesson history from disk."""
        path = self._history_path(lesson_id)
        if not path.exists():
            return None

        with open(path) as f:
            data = json.load(f)
        return LessonHistory.from_dict(data)

    def _save_history(self, history: LessonHistory) -> None:
        """Save lesson history to disk."""
        path = self._history_path(history.lesson_id)
        with open(path, "w") as f:
            json.dump(history.to_dict(), f, indent=2)

    def suggest_refinement(
        self,
        lesson_id: str,
        suggester: str,
        category: str,
        suggestion: str,
        priority: str = "medium",
    ) -> RefinementSuggestion:
        """Propose a refinement to a lesson."""
        refinement = RefinementSuggestion(
            lesson_id=lesson_id,
            suggester=suggester,
            timestamp=datetime.now().isoformat(),
            category=category,
            suggestion=suggestion,
            priority=priority,
            status="proposed",
        )

        # Load existing refinements
        refinements = self.load_refinements(lesson_id)
        refinements.append(refinement)

        # Save
        path = self._refinements_path(lesson_id)
        with open(path, "w") as f:
            json.dump([r.to_dict() for r in refinements], f, indent=2)

        return refinement

    def load_refinements(self, lesson_id: str) -> list[RefinementSuggestion]:
        """Load all refinement suggestions for a lesson."""
        path = self._refinements_path(lesson_id)
        if not path.exists():
            return []

        with open(path) as f:
            data = json.load(f)
        return [RefinementSuggestion.from_dict(r) for r in data]

    def update_refinement_status(
        self,
        lesson_id: str,
        suggestion_index: int,
        new_status: str,
    ) -> None:
        """Update status of a refinement suggestion."""
        refinements = self.load_refinements(lesson_id)
        if suggestion_index >= len(refinements):
            raise ValueError(f"Invalid suggestion index: {suggestion_index}")

        refinements[suggestion_index].status = new_status

        path = self._refinements_path(lesson_id)
        with open(path, "w") as f:
            json.dump([r.to_dict() for r in refinements], f, indent=2)

    def compare_versions(
        self,
        lesson_id: str,
        version1: int,
        version2: int,
    ) -> dict:
        """Compare two versions of a lesson."""
        history = self.load_history(lesson_id)
        if not history:
            raise ValueError(f"No history found for lesson {lesson_id}")

        v1 = history.get_version(version1)
        v2 = history.get_version(version2)

        if not v1 or not v2:
            raise ValueError(f"Invalid version numbers: {version1}, {version2}")

        return {
            "lesson_id": lesson_id,
            "version1": v1.to_dict(),
            "version2": v2.to_dict(),
            "content_changed": v1.content_hash != v2.content_hash,
        }

    def get_contributor_stats(self) -> dict[str, dict[str, int]]:
        """Get statistics about contributors across all lessons."""
        from typing import Any

        stats: dict[str, dict[str, Any]] = {}

        for history_file in self.history_dir.glob("*.json"):
            if history_file.parent.name == "refinements":
                continue

            history = self.load_history(history_file.stem)
            if not history:
                continue

            for contributor in history.contributors():
                if contributor not in stats:
                    stats[contributor] = {
                        "lessons_created": 0,
                        "versions_contributed": 0,
                        "lessons_contributed_to": set(),
                    }

                if contributor == history.origin_agent:
                    stats[contributor]["lessons_created"] += 1

                for version in history.versions:
                    if version.contributor == contributor:
                        stats[contributor]["versions_contributed"] += 1
                        stats[contributor]["lessons_contributed_to"].add(
                            history.lesson_id
                        )

        # Convert sets to counts
        for contributor in stats:
            stats[contributor]["lessons_contributed_to"] = len(
                stats[contributor]["lessons_contributed_to"]
            )

        return stats


def main():
    """CLI interface for evolution tracking."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: evolution.py <command> [args]")
        print("\nCommands:")
        print("  init <lesson-id> <agent-id> <content-file>  - Initialize tracking")
        print("  track <lesson-id> <agent-id> <changes> <content-file>  - Track change")
        print("  history <lesson-id>  - Show version history")
        print(
            "  suggest <lesson-id> <agent-id> <category> <suggestion>  - Suggest refinement"
        )
        print("  refinements <lesson-id>  - Show refinement suggestions")
        print("  stats  - Show contributor statistics")
        return

    tracker = EvolutionTracker()
    command = sys.argv[1]

    if command == "init":
        lesson_id, agent_id, content_file = sys.argv[2:5]
        with open(content_file) as f:
            content = f.read()
        history = tracker.initialize_lesson(lesson_id, agent_id, content)
        print(f"Initialized tracking for {lesson_id}")
        latest = history.latest_version()
        if latest:
            print(f"Version: {latest.version}")

    elif command == "track":
        lesson_id, agent_id, changes, content_file = sys.argv[2:6]
        with open(content_file) as f:
            content = f.read()
        version = tracker.track_change(lesson_id, agent_id, changes, content)
        print(f"Tracked change to {lesson_id}")
        print(f"New version: {version.version}")
        print(f"Contributor: {version.contributor}")

    elif command == "history":
        lesson_id = sys.argv[2]
        lesson_history = tracker.load_history(lesson_id)
        if not lesson_history:
            print(f"No history found for {lesson_id}")
            return

        print(f"Lesson: {lesson_history.lesson_id}")
        print(f"Origin: {lesson_history.origin_agent} ({lesson_history.created})")
        print(f"\nVersions ({len(lesson_history.versions)}):")
        for v in lesson_history.versions:
            print(f"  v{v.version}: {v.changes} ({v.contributor}, {v.timestamp})")

    elif command == "suggest":
        lesson_id, agent_id, category, suggestion = sys.argv[2:6]
        refinement = tracker.suggest_refinement(
            lesson_id, agent_id, category, suggestion
        )
        print(f"Suggested refinement for {lesson_id}")
        print(f"Category: {refinement.category}")
        print(f"Priority: {refinement.priority}")

    elif command == "refinements":
        lesson_id = sys.argv[2]
        refinements = tracker.load_refinements(lesson_id)
        if not refinements:
            print(f"No refinements found for {lesson_id}")
            return

        print(f"Refinement Suggestions for {lesson_id}:")
        for i, r in enumerate(refinements):
            print(f"\n{i}. [{r.status}] {r.category} ({r.priority})")
            print(f"   By: {r.suggester} ({r.timestamp})")
            print(f"   {r.suggestion}")

    elif command == "stats":
        stats = tracker.get_contributor_stats()
        print("Contributor Statistics:")
        for contributor, data in sorted(stats.items()):
            print(f"\n{contributor}:")
            print(f"  Lessons created: {data['lessons_created']}")
            print(f"  Versions contributed: {data['versions_contributed']}")
            print(f"  Lessons contributed to: {data['lessons_contributed_to']}")

    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
