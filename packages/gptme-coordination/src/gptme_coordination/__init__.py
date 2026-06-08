"""Generic inter-agent coordination: work claims, message bus, shared SQLite DB."""

from gptme_coordination.db import CoordinationDB, resolve_coordination_db_path
from gptme_coordination.messages import Message, MessageBus
from gptme_coordination.work import WorkClaim, WorkClaimManager

__all__ = [
    "CoordinationDB",
    "resolve_coordination_db_path",
    "Message",
    "MessageBus",
    "WorkClaim",
    "WorkClaimManager",
]
