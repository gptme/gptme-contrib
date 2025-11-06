"""Configuration file loaders for .env and YAML formats."""

import os
from pathlib import Path
from typing import Any, Dict


class DotEnvLoader:
    """
    Simple .env file loader.

    Loads environment variables from .env file without requiring python-dotenv.
    Does not override existing environment variables.
    """

    def __init__(self, workspace_dir: Path, env_file: str = ".env"):
        """
        Initialize .env loader.

        Args:
            workspace_dir: Directory containing .env file
            env_file: Name of .env file (default: ".env")
        """
        self.env_path = workspace_dir / env_file
        self._vars: Dict[str, str] = {}

    def load(self) -> Dict[str, str]:
        """
        Load variables from .env file.

        Returns:
            Dictionary of environment variables loaded from file

        Note:
            Variables are also set in os.environ if not already present.
        """
        if not self.env_path.exists():
            return {}

        with self.env_path.open("r") as f:
            for line in f:
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                # Parse key=value
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]

                # Store and set in environment if not already present
                self._vars[key] = value
                os.environ.setdefault(key, value)

        return self._vars

    def get(self, key: str, default: str | None = None) -> str | None:
        """
        Get a loaded environment variable.

        Args:
            key: Variable name
            default: Default value if not found

        Returns:
            Value from .env file, or default if not found
        """
        return self._vars.get(key, default)


class YAMLLoader:
    """
    YAML configuration file loader.

    Uses PyYAML if available, otherwise falls back to basic parsing.
    """

    def __init__(self, config_path: Path):
        """
        Initialize YAML loader.

        Args:
            config_path: Path to YAML config file
        """
        self.config_path = config_path
        self._config: Dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        """
        Load configuration from YAML file.

        Returns:
            Dictionary of configuration values

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If YAML parsing fails
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        try:
            import yaml

            with self.config_path.open("r") as f:
                self._config = yaml.safe_load(f) or {}
        except ImportError:
            # Fallback: basic YAML parsing (for simple configs)
            self._config = self._parse_simple_yaml()

        return self._config

    def _parse_simple_yaml(self) -> Dict[str, Any]:
        """
        Simple YAML parser for basic configs without PyYAML dependency.

        Limitations:
        - Only supports simple key: value pairs
        - Supports nested dicts with indentation
        - Does not support lists, multiline strings, or advanced features

        Returns:
            Parsed configuration dictionary
        """
        config: Dict[str, Any] = {}
        stack: list[tuple[int, Dict[str, Any]]] = [(0, config)]

        with self.config_path.open("r") as f:
            for line in f:
                # Skip comments and empty lines
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # Calculate indentation
                indent = len(line) - len(line.lstrip())

                # Pop stack to correct level
                while stack and indent <= stack[-1][0]:
                    stack.pop()

                # Parse key: value
                if ": " in line:
                    key, value_str = line.split(": ", 1)
                    key = key.strip()
                    value_str = value_str.strip()

                    # Remove quotes
                    if value_str.startswith('"') and value_str.endswith('"'):
                        value_str = value_str[1:-1]
                    elif value_str.startswith("'") and value_str.endswith("'"):
                        value_str = value_str[1:-1]

                    # Convert types
                    value: Any
                    if value_str.lower() == "true":
                        value = True
                    elif value_str.lower() == "false":
                        value = False
                    elif value_str.isdigit():
                        value = int(value_str)
                    elif value_str.replace(".", "", 1).isdigit():
                        value = float(value_str)
                    else:
                        value = value_str

                    # Add to current dict
                    current_dict = stack[-1][1]
                    current_dict[key] = value

                    # If value is empty, this might be a nested dict
                    if not value_str:
                        new_dict: Dict[str, Any] = {}
                        current_dict[key] = new_dict
                        stack.append((indent, new_dict))

        return config

    def get(self, path: str, default: Any = None) -> Any:
        """
        Get value from config using dot notation.

        Args:
            path: Dot-separated path (e.g., "evaluation.topics")
            default: Default value if path not found

        Returns:
            Value at path, or default if not found
        """
        keys = path.split(".")
        value = self._config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value


def load_multiple_env_files(workspace_dir: Path, env_files: list[str]) -> Dict[str, str]:
    """
    Load multiple .env files in order.

    Later files override earlier files.

    Args:
        workspace_dir: Directory containing .env files
        env_files: List of .env filenames to load in order

    Returns:
        Combined dictionary of environment variables
    """
    combined: Dict[str, str] = {}

    for env_file in env_files:
        loader = DotEnvLoader(workspace_dir, env_file)
        loaded = loader.load()
        combined.update(loaded)

    return combined
