"""
Shared data structures for the learning system.

Contains ConversationAnalysis dataclass used by the analyzer.
Episodes and experiences are represented as Dict[str, Any] for
flexibility and direct JSON serialization.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ConversationAnalysis:
    """Complete analysis of a conversation."""

    conversation_id: str
    timestamp: datetime
    duration_minutes: Optional[float]
    message_count: int
    user_messages: int
    assistant_messages: int
    tool_uses: Dict[str, int]
    files_modified: List[str]
    summary: str
    outcomes: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


# Removed: LessonDraft, Episode, LearnableMoment dataclasses
# These were designed for structured data but we now work with Dict[str, Any]
# for flexibility and direct JSON serialization.
# Only ConversationAnalysis dataclass remains as it's used by analyzer.
