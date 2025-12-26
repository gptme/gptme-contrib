"""Configuration management for input sources.

Centralized configuration loading and validation using Pydantic for type safety.
All paths and identifiers are configurable via environment variables.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


def get_workspace_path() -> Path:
    """Get workspace path from environment or default."""
    if path := os.environ.get("GPTME_WORKSPACE"):
        return Path(path)
    return Path.home() / "workspace"


def get_agent_config_dir() -> Path:
    """Get agent config directory from environment or default."""
    if path := os.environ.get("GPTME_CONFIG_DIR"):
        return Path(path)
    return Path.home() / ".config" / "gptme-agent"


def get_agent_data_dir() -> Path:
    """Get agent data directory from environment or default."""
    if path := os.environ.get("GPTME_DATA_DIR"):
        return Path(path)
    return Path.home() / ".local" / "share" / "gptme-agent"


def get_default_repo() -> str:
    """Get default GitHub repo from environment or default."""
    return os.environ.get("GPTME_AGENT_REPO", "owner/agent")


def get_maildir_path() -> Path:
    """Get maildir path from environment or default."""
    if path := os.environ.get("MAILDIR_PATH"):
        return Path(path)
    return Path.home() / ".local" / "share" / "mail" / "agent"


class RateLimitConfig(BaseModel):
    """Rate limiting configuration.

    Attributes:
        max_requests_per_minute: Maximum requests allowed per minute (minimum 1)
        max_requests_per_hour: Maximum requests allowed per hour (minimum 1)
        enabled: Whether rate limiting is enabled
    """

    max_requests_per_minute: int = Field(default=60, ge=1)
    max_requests_per_hour: int = Field(default=1000, ge=1)
    enabled: bool = Field(default=True)


class GitHubSourceConfig(BaseModel):
    """Configuration for GitHub input source.

    Attributes:
        enabled: Whether this source is enabled
        repo: GitHub repository in owner/name format
        label: Label to filter issues by
        workspace_path: Path to workspace directory
        poll_interval_seconds: How often to poll for new issues (minimum 60s)
        priority_labels: Labels that mark issues as high priority
        exclude_labels: Labels that exclude issues from processing
        rate_limit: Rate limiting configuration for this source
    """

    enabled: bool = Field(default=True)
    repo: str = Field(default_factory=get_default_repo)
    label: str = Field(default="task-request")
    workspace_path: Path = Field(default_factory=get_workspace_path)
    poll_interval_seconds: int = Field(default=300, ge=60)
    priority_labels: List[str] = Field(
        default_factory=lambda: ["priority:urgent", "priority:high"]
    )
    exclude_labels: List[str] = Field(default_factory=lambda: ["wontfix"])
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @field_validator("workspace_path", mode="before")
    @classmethod
    def parse_path(cls, v):
        """Convert string to Path if needed."""
        if isinstance(v, str):
            return Path(v)
        return v


class EmailSourceConfig(BaseModel):
    """Configuration for email input source.

    Attributes:
        enabled: Whether this source is enabled
        maildir_path: Path to maildir directory for reading emails
        allowlist_file: Path to file containing allowed email addresses
        workspace_path: Path to workspace directory
        poll_interval_seconds: How often to check for new emails (minimum 60s)
        rate_limit: Rate limiting configuration for this source
    """

    enabled: bool = Field(default=True)
    maildir_path: Path = Field(default_factory=get_maildir_path)
    allowlist_file: Path = Field(default_factory=lambda: get_workspace_path() / ".env")
    workspace_path: Path = Field(default_factory=get_workspace_path)
    poll_interval_seconds: int = Field(default=300, ge=60)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @field_validator("maildir_path", "allowlist_file", "workspace_path", mode="before")
    @classmethod
    def parse_paths(cls, v):
        """Convert string to Path if needed."""
        if isinstance(v, str):
            return Path(v)
        return v


class WebhookSourceConfig(BaseModel):
    """Configuration for webhook input source.

    Attributes:
        enabled: Whether this source is enabled
        queue_dir: Directory for webhook queue storage
        workspace_path: Path to workspace directory
        poll_interval_seconds: How often to check queue (minimum 10s)
        auth_token: Optional authentication token for webhooks
        rate_limit: Rate limiting configuration for this source
    """

    enabled: bool = Field(default=True)
    queue_dir: Path = Field(default=Path.home() / ".local/share/webhook-queue")
    workspace_path: Path = Field(default_factory=get_workspace_path)
    poll_interval_seconds: int = Field(default=60, ge=10)
    auth_token: Optional[str] = Field(default=None)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @field_validator("queue_dir", "workspace_path", mode="before")
    @classmethod
    def parse_paths(cls, v):
        """Convert string to Path if needed."""
        if isinstance(v, str):
            return Path(v)
        return v


class SchedulerSourceConfig(BaseModel):
    """Configuration for scheduler input source.

    Attributes:
        enabled: Whether this source is enabled
        schedule_file: Path to YAML file defining scheduled tasks
        state_file: Path to JSON file tracking scheduler state
        workspace_path: Path to workspace directory
        check_interval_seconds: How often to check schedule (minimum 30s)
        rate_limit: Rate limiting configuration for this source
    """

    enabled: bool = Field(default=True)
    schedule_file: Path = Field(
        default_factory=lambda: get_agent_config_dir() / "schedule.yaml"
    )
    state_file: Path = Field(
        default_factory=lambda: get_agent_data_dir() / "schedule-state.json"
    )
    workspace_path: Path = Field(default_factory=get_workspace_path)
    check_interval_seconds: int = Field(default=60, ge=30)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @field_validator("schedule_file", "state_file", "workspace_path", mode="before")
    @classmethod
    def parse_paths(cls, v):
        """Convert string to Path if needed."""
        if isinstance(v, str):
            return Path(v)
        return v


class MonitoringConfig(BaseModel):
    """Configuration for monitoring and health checks.

    Attributes:
        enabled: Whether monitoring is enabled
        metrics_dir: Directory for storing metrics data
        health_check_interval_seconds: How often to perform health checks (minimum 60s)
        max_consecutive_failures: Maximum consecutive failures before alert (minimum 1)
        log_level: Logging level (e.g., INFO, DEBUG, WARNING)
    """

    enabled: bool = Field(default=True)
    metrics_dir: Path = Field(default_factory=lambda: get_agent_data_dir() / "metrics")
    health_check_interval_seconds: int = Field(default=300, ge=60)
    max_consecutive_failures: int = Field(default=3, ge=1)
    log_level: str = Field(default="INFO")

    @field_validator("metrics_dir", mode="before")
    @classmethod
    def parse_path(cls, v):
        """Convert string to Path if needed."""
        if isinstance(v, str):
            return Path(v)
        return v


class InputSourcesConfig(BaseModel):
    """Top-level configuration for all input sources.

    Attributes:
        github: Configuration for GitHub issue source
        email: Configuration for email source
        webhook: Configuration for webhook source
        scheduler: Configuration for scheduled task source
        monitoring: Configuration for monitoring and health checks
    """

    github: GitHubSourceConfig = Field(default_factory=GitHubSourceConfig)
    email: EmailSourceConfig = Field(default_factory=EmailSourceConfig)
    webhook: WebhookSourceConfig = Field(default_factory=WebhookSourceConfig)
    scheduler: SchedulerSourceConfig = Field(default_factory=SchedulerSourceConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "InputSourcesConfig":
        """Load configuration from dictionary.

        Args:
            config_dict: Configuration dictionary

        Returns:
            Validated InputSourcesConfig instance
        """
        return cls(**config_dict)

    @classmethod
    def from_yaml(cls, config_path: Path) -> "InputSourcesConfig":
        """Load configuration from YAML file.

        Args:
            config_path: Path to YAML configuration file

        Returns:
            Validated InputSourcesConfig instance
        """
        import yaml

        with open(config_path) as f:
            config_dict = yaml.safe_load(f)

        return cls.from_dict(config_dict or {})

    @classmethod
    def from_toml(cls, config_path: Path) -> "InputSourcesConfig":
        """Load configuration from TOML file.

        Args:
            config_path: Path to TOML configuration file

        Returns:
            Validated InputSourcesConfig instance
        """
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef,import-not-found]

        with open(config_path, "rb") as f:
            config_dict = tomllib.load(f)

        return cls.from_dict(config_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration to dictionary with Path objects as strings.

        Returns:
            Configuration as dictionary
        """
        data = self.model_dump()

        # Convert Path objects to strings for YAML serialization
        def convert_paths(obj):
            if isinstance(obj, dict):
                return {k: convert_paths(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_paths(item) for item in obj]
            elif isinstance(obj, Path):
                return str(obj)
            return obj

        result = convert_paths(data)
        return dict(result) if isinstance(result, dict) else {}

    def to_yaml(self, config_path: Path) -> None:
        """Export configuration to YAML file.

        Args:
            config_path: Path to write YAML configuration
        """
        import yaml

        config_dict = self.to_dict()
        with open(config_path, "w") as f:
            yaml.safe_dump(config_dict, f, default_flow_style=False)
