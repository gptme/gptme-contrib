"""Platform-specific configuration classes."""

from pathlib import Path
from typing import Optional

from .base import BaseConfig


class EmailConfig(BaseConfig):
    """
    Configuration for email system.

    Environment Variables:
    - AGENT_EMAIL: Agent's email address (default: bob@superuserlabs.org)
    - MAILDIR_INBOX: Path to inbox maildir
    - MAILDIR_SENT: Path to sent maildir
    - EMAIL_ALLOWLIST: Comma-separated list of allowed sender addresses
    """

    def __init__(
        self,
        workspace_dir: Optional[Path] = None,
        env_file: str = ".env",
    ):
        """Initialize email configuration."""
        super().__init__(workspace_dir=workspace_dir, env_file=env_file)

        # Load email-specific configuration
        self.agent_email = (
            self.get_env("AGENT_EMAIL", default="bob@superuserlabs.org") or "bob@superuserlabs.org"
        )

        inbox_path = self.get_env(
            "MAILDIR_INBOX",
            default=str(Path.home() / ".local/share/mail/gmail/Bob"),
        ) or str(Path.home() / ".local/share/mail/gmail/Bob")
        self.maildir_inbox = Path(inbox_path)

        sent_path = self.get_env(
            "MAILDIR_SENT",
            default=str(Path.home() / ".local/share/mail/gmail/Bob/Sent"),
        ) or str(Path.home() / ".local/share/mail/gmail/Bob/Sent")
        self.maildir_sent = Path(sent_path)

        # Allowlist for auto-response
        allowlist_str = self.get_env("EMAIL_ALLOWLIST", default="") or ""
        self.allowlist = [email.strip() for email in allowlist_str.split(",") if email.strip()]

    def validate(self) -> tuple[bool, str]:
        """
        Validate email configuration.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check maildir paths exist
        if not self.maildir_inbox.exists():
            return False, f"Maildir inbox not found: {self.maildir_inbox}"

        if not self.maildir_sent.exists():
            return False, f"Maildir sent not found: {self.maildir_sent}"

        # Check email format
        if self.agent_email and "@" not in self.agent_email:
            return False, f"Invalid email address: {self.agent_email}"

        return True, ""

    def to_dict(self):
        """Convert to dictionary."""
        base = super().to_dict()
        base.update(
            {
                "agent_email": self.agent_email,
                "maildir_inbox": str(self.maildir_inbox),
                "maildir_sent": str(self.maildir_sent),
                "allowlist": self.allowlist,
            }
        )
        return base


class TwitterConfig(BaseConfig):
    """
    Configuration for Twitter system.

    Supports both .env and YAML configuration.

    Environment Variables:
    - TWITTER_BEARER_TOKEN: Twitter API bearer token (required)
    - TWITTER_API_KEY: Twitter API key
    - TWITTER_API_SECRET: Twitter API secret
    - TWITTER_ACCESS_TOKEN: Twitter access token
    - TWITTER_ACCESS_SECRET: Twitter access token secret

    YAML Config (config/config.yml):
    - evaluation.topics: List of relevant topics
    - evaluation.projects: List of project names
    - blacklist.topics: List of forbidden topics
    - blacklist.patterns: List of spam patterns
    """

    def __init__(
        self,
        workspace_dir: Optional[Path] = None,
        env_file: str = ".env",
        config_file: Optional[Path] = None,
    ):
        """
        Initialize Twitter configuration.

        Args:
            workspace_dir: Workspace directory
            env_file: .env file name
            config_file: Path to YAML config (defaults to config/config.yml)
        """
        # Default config file location
        if config_file is None:
            script_dir = Path(__file__).parent.parent.parent
            config_file = script_dir / "config" / "config.yml"

        super().__init__(
            workspace_dir=workspace_dir,
            env_file=env_file,
            config_file=config_file,
        )

        # Load Twitter API credentials
        self.bearer_token = self.get_env("TWITTER_BEARER_TOKEN")
        self.api_key = self.get_env("TWITTER_API_KEY")
        self.api_secret = self.get_env("TWITTER_API_SECRET")
        self.access_token = self.get_env("TWITTER_ACCESS_TOKEN")
        self.access_secret = self.get_env("TWITTER_ACCESS_SECRET")

        # Load YAML config if available
        self.topics = self.get_yaml("evaluation.topics", default=[])
        self.projects = self.get_yaml("evaluation.projects", default=[])
        self.triggers = self.get_yaml("evaluation.triggers", default={})
        self.blacklist_topics = self.get_yaml("blacklist.topics", default=[])
        self.blacklist_patterns = self.get_yaml("blacklist.patterns", default=[])

    def validate(self) -> tuple[bool, str]:
        """
        Validate Twitter configuration.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # At minimum, need bearer token
        if not self.bearer_token:
            return False, "TWITTER_BEARER_TOKEN not set"

        # If using OAuth1, need all four credentials
        if self.api_key or self.api_secret or self.access_token or self.access_secret:
            missing = []
            if not self.api_key:
                missing.append("TWITTER_API_KEY")
            if not self.api_secret:
                missing.append("TWITTER_API_SECRET")
            if not self.access_token:
                missing.append("TWITTER_ACCESS_TOKEN")
            if not self.access_secret:
                missing.append("TWITTER_ACCESS_SECRET")

            if missing:
                return False, f"Incomplete OAuth1 credentials, missing: {', '.join(missing)}"

        return True, ""

    def to_dict(self):
        """Convert to dictionary."""
        base = super().to_dict()
        base.update(
            {
                "bearer_token": "***" if self.bearer_token else None,  # Redacted
                "api_key": "***" if self.api_key else None,
                "topics": self.topics,
                "projects": self.projects,
                "blacklist_topics": self.blacklist_topics,
            }
        )
        return base


class DiscordConfig(BaseConfig):
    """
    Configuration for Discord bot.

    Environment Variables:
    - DISCORD_TOKEN: Discord bot token (required)
    - MODEL: LLM model to use (default: "anthropic")
    - RATE_LIMIT: Seconds between messages (default: 1.0)
    - ENABLE_PRIVILEGED_INTENTS: Enable privileged intents (default: false)
    """

    def __init__(
        self,
        workspace_dir: Optional[Path] = None,
        env_file: str = ".env.discord",
    ):
        """
        Initialize Discord configuration.

        Args:
            workspace_dir: Workspace directory
            env_file: .env file name (default: ".env.discord")
        """
        # Try loading from multiple .env files
        env_files = [".env", ".env.discord"]
        super().__init__(workspace_dir=workspace_dir, env_file=env_files[0])

        # Load from secondary .env file if present
        if len(env_files) > 1:
            from .loaders import DotEnvLoader

            for env_file in env_files[1:]:
                loader = DotEnvLoader(self.workspace_dir, env_file)
                extra_vars = loader.load()
                self._env_vars.update(extra_vars)

        # Load Discord-specific configuration
        self.token = self.get_env("DISCORD_TOKEN", required=True)
        self.model = self.get_env("MODEL", default="anthropic")
        self.rate_limit = self.get_env_float("RATE_LIMIT", default=1.0)
        self.enable_privileged_intents = self.get_env_bool(
            "ENABLE_PRIVILEGED_INTENTS", default=False
        )

    def validate(self) -> tuple[bool, str]:
        """
        Validate Discord configuration.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check token is set
        if not self.token:
            return False, "DISCORD_TOKEN not set in .env or .env.discord"

        # Check token looks valid (basic check)
        if len(self.token) < 50:
            return False, "DISCORD_TOKEN not properly configured"

        # Check rate limit is positive
        if self.rate_limit <= 0:
            return False, f"Invalid RATE_LIMIT: {self.rate_limit} (must be > 0)"

        return True, ""

    def to_dict(self):
        """Convert to dictionary."""
        base = super().to_dict()
        base.update(
            {
                "token": "***",  # Redacted
                "model": self.model,
                "rate_limit": self.rate_limit,
                "enable_privileged_intents": self.enable_privileged_intents,
            }
        )
        return base
