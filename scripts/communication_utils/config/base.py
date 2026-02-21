"""Base configuration class for all communication platforms."""

import os
from pathlib import Path
from typing import Any, Dict


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""

    pass


class BaseConfig:
    """
    Base configuration class providing common functionality.

    Features:
    - Automatic .env loading from workspace
    - Support for default values
    - Type conversion
    - Validation hooks
    """

    def __init__(
        self,
        workspace_dir: Path | None = None,
        env_file: str | None = None,
        config_file: Path | None = None,
    ):
        """
        Initialize configuration.

        Args:
            workspace_dir: Workspace directory (defaults to current dir)
            env_file: Name of .env file (e.g., ".env", ".env.discord")
            config_file: Path to YAML config file (optional)
        """
        self.workspace_dir = workspace_dir or Path.cwd()
        self.env_file = env_file or ".env"
        self.config_file = config_file

        # Config storage
        self._env_vars: Dict[str, str] = {}
        self._yaml_config: Dict[str, Any] = {}

        # Load configuration
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from .env and/or YAML files."""
        # Load .env file if specified
        if self.env_file:
            from .loaders import DotEnvLoader

            env_loader = DotEnvLoader(self.workspace_dir, self.env_file)
            self._env_vars = env_loader.load()

        # Load YAML config if specified
        if self.config_file and self.config_file.exists():
            from .loaders import YAMLLoader

            yaml_loader = YAMLLoader(self.config_file)
            self._yaml_config = yaml_loader.load()

    def get_env(
        self, key: str, default: str | None = None, required: bool = False
    ) -> str | None:
        """
        Get environment variable with fallback to .env file.

        Args:
            key: Environment variable name
            default: Default value if not found
            required: Raise ConfigError if not found and no default

        Returns:
            Value from environment or .env file, or default

        Raises:
            ConfigError: If required=True and no value found
        """
        # Check os.environ first (highest priority)
        value = os.environ.get(key)

        # Fallback to loaded .env vars
        if value is None:
            value = self._env_vars.get(key)

        # Use default if still not found
        if value is None:
            value = default

        # Validate required
        if value is None and required:
            raise ConfigError(f"Required environment variable not set: {key}")

        return value

    def get_env_bool(self, key: str, default: bool = False) -> bool:
        """
        Get boolean environment variable.

        Args:
            key: Environment variable name
            default: Default value if not found

        Returns:
            Boolean value

        Accepts: "true", "1", "yes", "on" (case-insensitive) as True
        """
        value = self.get_env(key)
        if value is None:
            return default

        return value.lower() in ("true", "1", "yes", "on")

    def get_env_int(self, key: str, default: int = 0) -> int:
        """
        Get integer environment variable.

        Args:
            key: Environment variable name
            default: Default value if not found

        Returns:
            Integer value

        Raises:
            ConfigError: If value cannot be converted to int
        """
        value = self.get_env(key)
        if value is None:
            return default

        try:
            return int(value)
        except ValueError as e:
            raise ConfigError(f"Invalid integer value for {key}: {value}") from e

    def get_env_float(self, key: str, default: float = 0.0) -> float:
        """
        Get float environment variable.

        Args:
            key: Environment variable name
            default: Default value if not found

        Returns:
            Float value

        Raises:
            ConfigError: If value cannot be converted to float
        """
        value = self.get_env(key)
        if value is None:
            return default

        try:
            return float(value)
        except ValueError as e:
            raise ConfigError(f"Invalid float value for {key}: {value}") from e

    def get_yaml(self, path: str, default: Any = None) -> Any:
        """
        Get value from YAML config using dot notation.

        Args:
            path: Dot-separated path (e.g., "evaluation.topics")
            default: Default value if path not found

        Returns:
            Value at path, or default if not found

        Example:
            config.get_yaml("evaluation.topics")  # Access nested dict
        """
        if not self._yaml_config:
            return default

        keys = path.split(".")
        value = self._yaml_config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def validate(self) -> tuple[bool, str]:
        """
        Validate configuration.

        Override in subclasses to implement platform-specific validation.

        Returns:
            Tuple of (is_valid, error_message)
        """
        return True, ""

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert configuration to dictionary.

        Override in subclasses to include platform-specific fields.

        Returns:
            Dictionary representation of config
        """
        return {
            "workspace_dir": str(self.workspace_dir),
            "env_file": self.env_file,
            "config_file": str(self.config_file) if self.config_file else None,
            "env_vars": self._env_vars,
            "yaml_config": self._yaml_config,
        }
