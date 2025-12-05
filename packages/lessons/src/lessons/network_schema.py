"""
Network metadata schema for collaborative learning between agents.

Part of Phase 4.3 Phase 1: Local Infrastructure
Design: knowledge/technical-designs/agent-network-protocol.md
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class NetworkMetadata:
    """Network metadata for shared lessons across agent network.

    This metadata enables:
    - Unique identification across agent network
    - Attribution to originating agent
    - Quality metrics for adoption decisions
    - Usage tracking across the network
    """

    # Identification
    lesson_id: str  # Format: "{agent}-{sequence}-{slug}"
    agent_origin: str  # Which agent created this (e.g., "agent1", "agent2")

    # Timestamps
    created: datetime
    updated: datetime

    # Quality Metrics
    confidence: float  # 0.0-1.0: How confident is this lesson?
    success_rate: Optional[float] = None  # 0.0-1.0: Empirical effectiveness

    # Network Metrics
    adoption_count: int = 0  # How many agents use this?

    # Version & Compatibility
    schema_version: str = "1.0"  # Protocol version

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        return {
            "lesson_id": self.lesson_id,
            "agent_origin": self.agent_origin,
            "created": self.created.isoformat(),
            "updated": self.updated.isoformat(),
            "confidence": self.confidence,
            "success_rate": self.success_rate,
            "adoption_count": self.adoption_count,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NetworkMetadata":
        """Create from dictionary (YAML deserialization)."""
        return cls(
            lesson_id=data["lesson_id"],
            agent_origin=data["agent_origin"],
            created=datetime.fromisoformat(data["created"]),
            updated=datetime.fromisoformat(data["updated"]),
            confidence=data["confidence"],
            success_rate=data.get("success_rate"),
            adoption_count=data.get("adoption_count", 0),
            schema_version=data.get("schema_version", "1.0"),
        )

    @classmethod
    def generate_default(
        cls,
        lesson_path: str,
        agent_origin: str = "agent",
        confidence: float = 0.8,
    ) -> "NetworkMetadata":
        """Generate default network metadata for a lesson.

        Args:
            lesson_path: Path to lesson file (e.g., "lessons/workflow/autonomous-run.md")
            agent_origin: Which agent created this (default: "agent")
            confidence: Initial confidence score (default: 0.8)

        Returns:
            NetworkMetadata with generated lesson_id and timestamps
        """
        # Extract lesson slug from path
        # e.g., "lessons/workflow/autonomous-run.md" -> "autonomous-run"
        path_obj = Path(lesson_path)
        slug = path_obj.stem

        # Generate lesson_id: {agent}-{category}-{slug}
        category = path_obj.parent.name if path_obj.parent != Path(".") else "general"
        lesson_id = f"{agent_origin}-{category}-{slug}"

        now = datetime.now()

        return cls(
            lesson_id=lesson_id,
            agent_origin=agent_origin,
            created=now,
            updated=now,
            confidence=confidence,
            success_rate=None,  # Unknown until empirical data collected
            adoption_count=0,  # Local lesson, not yet shared
            schema_version="1.0",
        )


def validate_network_metadata(metadata: dict) -> tuple[bool, list[str]]:
    """Validate network metadata dictionary.

    Args:
        metadata: Dictionary with network metadata fields

    Returns:
        Tuple of (is_valid, error_messages)
    """
    errors = []

    # Required fields
    required = ["lesson_id", "agent_origin", "created", "updated", "confidence"]
    for field in required:
        if field not in metadata:
            errors.append(f"Missing required field: {field}")

    # Validate types and ranges
    if "confidence" in metadata:
        try:
            conf = float(metadata["confidence"])
            if not 0.0 <= conf <= 1.0:
                errors.append(f"Confidence must be 0.0-1.0, got {conf}")
        except (TypeError, ValueError):
            errors.append(f"Confidence must be numeric, got {metadata['confidence']}")

    if "success_rate" in metadata and metadata["success_rate"] is not None:
        try:
            rate = float(metadata["success_rate"])
            if not 0.0 <= rate <= 1.0:
                errors.append(f"Success rate must be 0.0-1.0, got {rate}")
        except (TypeError, ValueError):
            errors.append(
                f"Success rate must be numeric, got {metadata['success_rate']}"
            )

    if "adoption_count" in metadata:
        try:
            count = int(metadata["adoption_count"])
            if count < 0:
                errors.append(f"Adoption count must be >= 0, got {count}")
        except (TypeError, ValueError):
            errors.append(
                f"Adoption count must be integer, got {metadata['adoption_count']}"
            )

    # Validate timestamps
    for field in ["created", "updated"]:
        if field in metadata:
            try:
                datetime.fromisoformat(metadata[field])
            except (TypeError, ValueError):
                errors.append(
                    f"{field} must be ISO 8601 timestamp, got {metadata[field]}"
                )

    return (len(errors) == 0, errors)
