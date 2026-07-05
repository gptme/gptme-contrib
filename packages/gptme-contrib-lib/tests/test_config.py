"""Tests for config module.

Tests Pydantic configuration models, path resolution, validation, and serialization.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_contrib_lib.config import (
    EmailSourceConfig,
    GitHubSourceConfig,
    InputSourcesConfig,
    MonitoringConfig,
    RateLimitConfig,
    SchedulerSourceConfig,
    WebhookSourceConfig,
    get_agent_config_dir,
    get_agent_data_dir,
    get_default_repo,
    get_maildir_path,
    get_workspace_path,
)

# ── Helper functions ────────────────────────────────────────────


class TestHelperFunctions:
    """Test workspace/config path resolution."""

    def test_get_workspace_path_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GPTME_WORKSPACE", None)
            path = get_workspace_path()
            assert path == Path.home() / "workspace"

    def test_get_workspace_path_from_env(self):
        with patch.dict(os.environ, {"GPTME_WORKSPACE": "/custom/workspace"}):
            path = get_workspace_path()
            assert path == Path("/custom/workspace")

    def test_get_agent_config_dir_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GPTME_CONFIG_DIR", None)
            path = get_agent_config_dir()
            assert path == Path.home() / ".config" / "gptme-agent"

    def test_get_agent_config_dir_from_env(self):
        with patch.dict(os.environ, {"GPTME_CONFIG_DIR": "/etc/agent"}):
            path = get_agent_config_dir()
            assert path == Path("/etc/agent")

    def test_get_agent_data_dir_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GPTME_DATA_DIR", None)
            path = get_agent_data_dir()
            assert path == Path.home() / ".local" / "share" / "gptme-agent"

    def test_get_agent_data_dir_from_env(self):
        with patch.dict(os.environ, {"GPTME_DATA_DIR": "/data/agent"}):
            path = get_agent_data_dir()
            assert path == Path("/data/agent")

    def test_get_default_repo(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GPTME_AGENT_REPO", None)
            assert get_default_repo() == "owner/agent"

    def test_get_default_repo_from_env(self):
        with patch.dict(os.environ, {"GPTME_AGENT_REPO": "myorg/myagent"}):
            assert get_default_repo() == "myorg/myagent"

    def test_get_maildir_path_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MAILDIR_PATH", None)
            path = get_maildir_path()
            assert path == Path.home() / ".local" / "share" / "mail" / "agent"

    def test_get_maildir_path_from_env(self):
        with patch.dict(os.environ, {"MAILDIR_PATH": "/var/mail/bot"}):
            path = get_maildir_path()
            assert path == Path("/var/mail/bot")


# ── RateLimitConfig ─────────────────────────────────────────────


class TestRateLimitConfig:
    """Test rate limit configuration model."""

    def test_defaults(self):
        cfg = RateLimitConfig()
        assert cfg.max_requests_per_minute == 60
        assert cfg.max_requests_per_hour == 1000
        assert cfg.enabled is True

    def test_custom_values(self):
        cfg = RateLimitConfig(
            max_requests_per_minute=10, max_requests_per_hour=100, enabled=False
        )
        assert cfg.max_requests_per_minute == 10
        assert cfg.enabled is False

    def test_validation_min_values(self):
        with pytest.raises(Exception):
            RateLimitConfig(max_requests_per_minute=0)

        with pytest.raises(Exception):
            RateLimitConfig(max_requests_per_hour=0)


# ── GitHubSourceConfig ──────────────────────────────────────────


class TestGitHubSourceConfig:
    """Test GitHub source configuration."""

    def test_defaults(self):
        cfg = GitHubSourceConfig()
        assert cfg.enabled is True
        assert cfg.label == "task-request"
        assert cfg.poll_interval_seconds == 300
        assert isinstance(cfg.priority_labels, list)
        assert isinstance(cfg.exclude_labels, list)

    def test_workspace_path_string_conversion(self):
        cfg = GitHubSourceConfig(workspace_path="/tmp/test")
        assert isinstance(cfg.workspace_path, Path)
        assert cfg.workspace_path == Path("/tmp/test")

    def test_poll_interval_minimum(self):
        with pytest.raises(Exception):
            GitHubSourceConfig(poll_interval_seconds=30)  # below 60

    def test_custom_labels(self):
        cfg = GitHubSourceConfig(
            priority_labels=["urgent"],
            exclude_labels=["spam", "invalid"],
        )
        assert cfg.priority_labels == ["urgent"]
        assert cfg.exclude_labels == ["spam", "invalid"]


# ── EmailSourceConfig ───────────────────────────────────────────


class TestEmailSourceConfig:
    """Test email source configuration."""

    def test_defaults(self):
        cfg = EmailSourceConfig()
        assert cfg.enabled is True
        assert cfg.poll_interval_seconds == 300

    def test_path_string_conversion(self):
        cfg = EmailSourceConfig(
            maildir_path="/var/mail/test",
            allowlist_file="/etc/allowlist",
            workspace_path="/home/agent",
        )
        assert isinstance(cfg.maildir_path, Path)
        assert isinstance(cfg.allowlist_file, Path)
        assert isinstance(cfg.workspace_path, Path)


# ── WebhookSourceConfig ────────────────────────────────────────


class TestWebhookSourceConfig:
    """Test webhook source configuration."""

    def test_defaults(self):
        cfg = WebhookSourceConfig()
        assert cfg.enabled is True
        assert cfg.auth_token is None
        assert cfg.poll_interval_seconds == 60

    def test_poll_interval_minimum(self):
        with pytest.raises(Exception):
            WebhookSourceConfig(poll_interval_seconds=5)  # below 10

    def test_with_auth_token(self):
        cfg = WebhookSourceConfig(auth_token="secret123")
        assert cfg.auth_token == "secret123"


# ── SchedulerSourceConfig ──────────────────────────────────────


class TestSchedulerSourceConfig:
    """Test scheduler source configuration."""

    def test_defaults(self):
        cfg = SchedulerSourceConfig()
        assert cfg.enabled is True
        assert cfg.check_interval_seconds == 60

    def test_check_interval_minimum(self):
        with pytest.raises(Exception):
            SchedulerSourceConfig(check_interval_seconds=10)  # below 30


# ── MonitoringConfig ────────────────────────────────────────────


class TestMonitoringConfig:
    """Test monitoring configuration."""

    def test_defaults(self):
        cfg = MonitoringConfig()
        assert cfg.enabled is True
        assert cfg.health_check_interval_seconds == 300
        assert cfg.max_consecutive_failures == 3
        assert cfg.log_level == "INFO"

    def test_custom_log_level(self):
        cfg = MonitoringConfig(log_level="DEBUG")
        assert cfg.log_level == "DEBUG"

    def test_metrics_dir_string_conversion(self):
        cfg = MonitoringConfig(metrics_dir="/tmp/metrics")
        assert isinstance(cfg.metrics_dir, Path)


# ── InputSourcesConfig ─────────────────────────────────────────


class TestInputSourcesConfig:
    """Test top-level configuration."""

    def test_defaults(self):
        cfg = InputSourcesConfig()
        assert isinstance(cfg.github, GitHubSourceConfig)
        assert isinstance(cfg.email, EmailSourceConfig)
        assert isinstance(cfg.webhook, WebhookSourceConfig)
        assert isinstance(cfg.scheduler, SchedulerSourceConfig)
        assert isinstance(cfg.monitoring, MonitoringConfig)

    def test_from_dict(self):
        data = {
            "github": {"repo": "myorg/myrepo", "label": "agent-task"},
            "monitoring": {"log_level": "DEBUG"},
        }
        cfg = InputSourcesConfig.from_dict(data)
        assert cfg.github.repo == "myorg/myrepo"
        assert cfg.github.label == "agent-task"
        assert cfg.monitoring.log_level == "DEBUG"
        # Others should have defaults
        assert cfg.email.enabled is True

    def test_from_dict_empty(self):
        cfg = InputSourcesConfig.from_dict({})
        assert cfg.github.enabled is True

    def test_to_dict_roundtrip(self):
        original = InputSourcesConfig(
            github=GitHubSourceConfig(repo="test/repo"),
            monitoring=MonitoringConfig(log_level="WARNING"),
        )
        data = original.to_dict()
        assert isinstance(data, dict)
        assert data["github"]["repo"] == "test/repo"
        assert data["monitoring"]["log_level"] == "WARNING"
        # Paths should be strings (not Path objects)
        assert isinstance(data["github"]["workspace_path"], str)

    def test_from_yaml(self, tmp_path):
        yaml_content = """
github:
  repo: "org/repo"
  label: "bot-request"
  enabled: true
monitoring:
  log_level: "WARNING"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)
        cfg = InputSourcesConfig.from_yaml(yaml_file)
        assert cfg.github.repo == "org/repo"
        assert cfg.github.label == "bot-request"
        assert cfg.monitoring.log_level == "WARNING"

    def test_from_toml(self, tmp_path):
        toml_content = """
[github]
repo = "org/repo"
label = "bot-request"
enabled = true

[monitoring]
log_level = "ERROR"
"""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(toml_content)
        cfg = InputSourcesConfig.from_toml(toml_file)
        assert cfg.github.repo == "org/repo"
        assert cfg.monitoring.log_level == "ERROR"

    def test_to_yaml_roundtrip(self, tmp_path):
        original = InputSourcesConfig(
            github=GitHubSourceConfig(repo="test/repo", label="custom"),
        )
        yaml_file = tmp_path / "output.yaml"
        original.to_yaml(yaml_file)
        assert yaml_file.exists()
        loaded = InputSourcesConfig.from_yaml(yaml_file)
        assert loaded.github.repo == "test/repo"
        assert loaded.github.label == "custom"
