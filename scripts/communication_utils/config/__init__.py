"""
Shared configuration utilities for communication platforms.

Provides unified configuration loading supporting both .env and YAML files,
with platform-specific configurations and validation.
"""

from .base import BaseConfig, ConfigError
from .loaders import DotEnvLoader, YAMLLoader
from .platform_configs import DiscordConfig, EmailConfig, TwitterConfig
from .validation import ConfigValidator

__all__ = [
    "BaseConfig",
    "ConfigError",
    "DotEnvLoader",
    "YAMLLoader",
    "EmailConfig",
    "TwitterConfig",
    "DiscordConfig",
    "ConfigValidator",
]
