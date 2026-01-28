"""Agent registry for multi-agent coordination.

Tracks which agents exist and their status via file-based coordination.
Agent status files are stored in state/agents/ directory.

Status file format: {agent_id}.status containing JSON:
{
    "agent_id": "bob-session-123",
    "instance_type": "autonomous",
    "started": "2026-01-28T08:00:00Z",
    "last_heartbeat": "2026-01-28T08:15:00Z",
    "current_task": "implement-feature-x",
    "tasks_completed": 3,
    "status": "working",
    "workspace": "/home/bob/bob"
}

Design doc: tools/gptodo/docs/DESIGN-multi-agent-coordination.md
Tracking: ErikBjare/bob#263
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Literal

logger = logging.getLogger(__name__)

# Default heartbeat timeout - agent considered stale after this
DEFAULT_HEARTBEAT_TIMEOUT_MINUTES = 30

# Agent status values
AgentStatus = Literal["starting", "idle", "working", "waiting", "stopping"]


@dataclass
class AgentInfo:
    """Information about a registered agent."""

    agent_id: str
    instance_type: str = "autonomous"
    started: str = ""
    last_heartbeat: str = ""
    current_task: Optional[str] = None
    tasks_completed: int = 0
    status: AgentStatus = "starting"
    workspace: str = ""

    def __post_init__(self):
        if not self.started:
            self.started = datetime.now(timezone.utc).isoformat()
        if not self.last_heartbeat:
            self.last_heartbeat = self.started

    def is_stale(self, timeout_minutes: int = DEFAULT_HEARTBEAT_TIMEOUT_MINUTES) -> bool:
        """Check if agent heartbeat has timed out."""
        try:
            heartbeat_time = datetime.fromisoformat(self.last_heartbeat.replace("Z", "+00:00"))
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
            return heartbeat_time < cutoff
        except (ValueError, TypeError):
            return True

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentInfo":
        """Create AgentInfo from dictionary."""
        # Filter to only known fields
        known_fields = {
            "agent_id",
            "instance_type",
            "started",
            "last_heartbeat",
            "current_task",
            "tasks_completed",
            "status",
            "workspace",
        }
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


def get_agents_dir(workspace: Optional[Path] = None) -> Path:
    """Get the agents status directory path.

    Args:
        workspace: Optional workspace root. Defaults to cwd.

    Returns:
        Path to state/agents/ directory
    """
    if workspace is None:
        workspace = Path.cwd()
    return workspace / "state" / "agents"


def register_agent(
    agent_id: str,
    workspace: Optional[Path] = None,
    instance_type: str = "autonomous",
) -> AgentInfo:
    """Register a new agent or update existing registration.

    Args:
        agent_id: Unique identifier for the agent instance
        workspace: Workspace root directory
        instance_type: Type of agent (autonomous, interactive, etc.)

    Returns:
        AgentInfo for the registered agent
    """
    agents_dir = get_agents_dir(workspace)
    agents_dir.mkdir(parents=True, exist_ok=True)

    status_file = agents_dir / f"{agent_id}.status"

    # Check if agent already exists
    existing = None
    if status_file.exists():
        try:
            existing = AgentInfo.from_dict(json.loads(status_file.read_text()))
        except (json.JSONDecodeError, KeyError):
            pass

    # Create or update agent info
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        # Update existing agent
        existing.last_heartbeat = now
        existing.status = "idle"
        agent_info = existing
    else:
        # Create new agent
        agent_info = AgentInfo(
            agent_id=agent_id,
            instance_type=instance_type,
            started=now,
            last_heartbeat=now,
            status="starting",
            workspace=str(workspace or Path.cwd()),
        )

    # Write status file
    status_file.write_text(json.dumps(agent_info.to_dict(), indent=2))
    logger.info(f"Registered agent: {agent_id}")

    return agent_info


def update_agent_status(
    agent_id: str,
    status: AgentStatus,
    current_task: Optional[str] = None,
    workspace: Optional[Path] = None,
) -> Optional[AgentInfo]:
    """Update agent status and heartbeat.

    Args:
        agent_id: Agent identifier
        status: New status value
        current_task: Task currently being worked on (or None)
        workspace: Workspace root directory

    Returns:
        Updated AgentInfo or None if agent not found
    """
    agents_dir = get_agents_dir(workspace)
    status_file = agents_dir / f"{agent_id}.status"

    if not status_file.exists():
        logger.warning(f"Agent not found: {agent_id}")
        return None

    try:
        agent_info = AgentInfo.from_dict(json.loads(status_file.read_text()))
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to read agent status: {e}")
        return None

    # Update fields
    agent_info.status = status
    agent_info.current_task = current_task
    agent_info.last_heartbeat = datetime.now(timezone.utc).isoformat()

    # Increment completed count when transitioning from working to idle
    # (This is a simple heuristic - could be made more sophisticated)

    # Write updated status
    status_file.write_text(json.dumps(agent_info.to_dict(), indent=2))

    return agent_info


def unregister_agent(agent_id: str, workspace: Optional[Path] = None) -> bool:
    """Remove agent registration.

    Args:
        agent_id: Agent identifier
        workspace: Workspace root directory

    Returns:
        True if agent was unregistered, False if not found
    """
    agents_dir = get_agents_dir(workspace)
    status_file = agents_dir / f"{agent_id}.status"

    if status_file.exists():
        status_file.unlink()
        logger.info(f"Unregistered agent: {agent_id}")
        return True
    return False


def list_agents(
    workspace: Optional[Path] = None,
    include_stale: bool = False,
    timeout_minutes: int = DEFAULT_HEARTBEAT_TIMEOUT_MINUTES,
) -> List[AgentInfo]:
    """List all registered agents.

    Args:
        workspace: Workspace root directory
        include_stale: Include agents with stale heartbeats
        timeout_minutes: Minutes before heartbeat is considered stale

    Returns:
        List of AgentInfo objects for registered agents
    """
    agents_dir = get_agents_dir(workspace)
    if not agents_dir.exists():
        return []

    agents = []
    for status_file in agents_dir.glob("*.status"):
        try:
            data = json.loads(status_file.read_text())
            agent_info = AgentInfo.from_dict(data)

            # Skip stale agents unless requested
            if not include_stale and agent_info.is_stale(timeout_minutes):
                continue

            agents.append(agent_info)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to read agent status {status_file}: {e}")
            continue

    # Sort by started time (newest first)
    agents.sort(key=lambda a: a.started, reverse=True)
    return agents


def cleanup_stale_agents(
    workspace: Optional[Path] = None,
    timeout_minutes: int = DEFAULT_HEARTBEAT_TIMEOUT_MINUTES,
) -> List[str]:
    """Remove stale agent registrations.

    Args:
        workspace: Workspace root directory
        timeout_minutes: Minutes before heartbeat is considered stale

    Returns:
        List of cleaned up agent IDs
    """
    agents_dir = get_agents_dir(workspace)
    if not agents_dir.exists():
        return []

    cleaned = []
    for status_file in agents_dir.glob("*.status"):
        try:
            data = json.loads(status_file.read_text())
            agent_info = AgentInfo.from_dict(data)

            if agent_info.is_stale(timeout_minutes):
                status_file.unlink()
                cleaned.append(agent_info.agent_id)
                logger.info(f"Cleaned up stale agent: {agent_info.agent_id}")
        except (json.JSONDecodeError, KeyError) as e:
            # Remove corrupt status files
            status_file.unlink()
            cleaned.append(status_file.stem)
            logger.warning(f"Removed corrupt agent status {status_file}: {e}")

    return cleaned


def get_agent(agent_id: str, workspace: Optional[Path] = None) -> Optional[AgentInfo]:
    """Get info for a specific agent.

    Args:
        agent_id: Agent identifier
        workspace: Workspace root directory

    Returns:
        AgentInfo or None if not found
    """
    agents_dir = get_agents_dir(workspace)
    status_file = agents_dir / f"{agent_id}.status"

    if not status_file.exists():
        return None

    try:
        data = json.loads(status_file.read_text())
        return AgentInfo.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return None
