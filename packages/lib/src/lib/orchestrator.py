#!/usr/bin/env python3
"""Input sources orchestrator.

Coordinates polling of all configured input sources and creates tasks
from external sources like GitHub issues, emails, webhooks, and scheduled triggers.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from .config import InputSourcesConfig
from .input_source_impl import (
    EmailInputSource,
    GitHubInputSource,
    SchedulerInputSource,
    WebhookInputSource,
)
from .input_sources import InputSource


class InputSourceOrchestrator:
    """Orchestrates polling and processing of all input sources."""

    def __init__(self, config: InputSourcesConfig):
        """Initialize orchestrator with configuration.

        Args:
            config: Validated configuration for all input sources
        """
        self.config = config
        self.sources: Dict[str, InputSource] = {}
        self.last_poll: Dict[str, datetime] = {}
        self.running = False

        # Set up logging
        log_level = getattr(logging, config.monitoring.log_level.upper())
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger(__name__)

        # Initialize sources
        self._initialize_sources()

    def _initialize_sources(self):
        """Initialize all enabled input sources."""
        # GitHub
        if self.config.github.enabled:
            self.sources["github"] = GitHubInputSource(
                config={
                    "repo": self.config.github.repo,
                    "label": self.config.github.label,
                    "workspace_path": str(self.config.github.workspace_path),
                }
            )
            self.last_poll["github"] = datetime.min
            self.logger.info("Initialized GitHub input source")

        # Email
        if self.config.email.enabled:
            # Load allowlist from .env file
            allowlist = self._load_email_allowlist()
            self.sources["email"] = EmailInputSource(
                config={
                    "maildir_path": str(self.config.email.maildir_path),
                    "allowlist": allowlist,
                    "workspace_path": str(self.config.email.workspace_path),
                }
            )
            self.last_poll["email"] = datetime.min
            self.logger.info(
                f"Initialized Email input source ({len(allowlist)} allowlist)"
            )

        # Webhook
        if self.config.webhook.enabled:
            self.sources["webhook"] = WebhookInputSource(
                config={
                    "webhook_queue_path": str(self.config.webhook.queue_dir),
                    "workspace_path": str(self.config.webhook.workspace_path),
                    "require_auth_token": self.config.webhook.auth_token,
                }
            )
            self.last_poll["webhook"] = datetime.min
            self.logger.info("Initialized Webhook input source")

        # Scheduler
        if self.config.scheduler.enabled:
            self.sources["scheduler"] = SchedulerInputSource(
                config={
                    "schedule_config_path": str(self.config.scheduler.schedule_file),
                    "workspace_path": str(self.config.scheduler.workspace_path),
                    "state_file_path": str(self.config.scheduler.state_file),
                }
            )
            self.last_poll["scheduler"] = datetime.min
            self.logger.info("Initialized Scheduler input source")

    def _load_email_allowlist(self) -> List[str]:
        """Load email allowlist from .env file.

        Returns:
            List of allowed email addresses
        """
        env_file = self.config.email.allowlist_file
        if not env_file.exists():
            self.logger.warning(f"Email allowlist file not found: {env_file}")
            return []

        allowlist = []
        try:
            content = env_file.read_text()
            for line in content.split("\n"):
                if line.startswith("EMAIL_ALLOWLIST="):
                    emails = line.split("=", 1)[1].strip().strip("\"'")
                    allowlist = [e.strip() for e in emails.split(",") if e.strip()]
                    break
        except Exception as e:
            self.logger.error(f"Failed to load email allowlist: {e}")

        return allowlist

    def _should_poll(self, source_name: str) -> bool:
        """Check if source should be polled based on interval.

        Args:
            source_name: Name of the source to check

        Returns:
            True if enough time has elapsed since last poll
        """
        last_poll = self.last_poll.get(source_name, datetime.min)
        current_time = datetime.now()

        # Get poll interval for this source
        interval_map = {
            "github": self.config.github.poll_interval_seconds,
            "email": self.config.email.poll_interval_seconds,
            "webhook": self.config.webhook.poll_interval_seconds,
            "scheduler": self.config.scheduler.check_interval_seconds,
        }

        interval = timedelta(seconds=interval_map.get(source_name, 300))
        return (current_time - last_poll) >= interval

    async def poll_source(self, source_name: str, source: InputSource) -> int:
        """Poll a single input source and process requests.

        Args:
            source_name: Name of the source
            source: InputSource instance to poll

        Returns:
            Number of tasks created
        """
        try:
            self.logger.debug(f"Polling {source_name}...")
            requests = await source.poll_for_inputs()

            if not requests:
                self.logger.debug(f"No new requests from {source_name}")
                return 0

            self.logger.info(f"Found {len(requests)} requests from {source_name}")
            tasks_created = 0

            for request in requests:
                # Validate request
                validation = source.validate_input(request)
                if not validation.is_valid:
                    self.logger.warning(
                        f"Validation failed for {source_name} request: {validation.message}"
                    )
                    continue

                # Create task
                result = await source.create_task(request)
                if result.success:
                    self.logger.info(
                        f"Created task from {source_name}: {result.task_id}"
                    )
                    tasks_created += 1

                    # Acknowledge input
                    try:
                        await source.acknowledge_input(request)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to acknowledge {source_name} input: {e}"
                        )
                else:
                    self.logger.error(
                        f"Failed to create task from {source_name}: {result.error}"
                    )

            return tasks_created

        except Exception as e:
            self.logger.error(f"Error polling {source_name}: {e}", exc_info=True)
            return 0
        finally:
            self.last_poll[source_name] = datetime.now()

    async def run_once(self) -> Dict[str, int]:
        """Run one iteration of polling all sources.

        Returns:
            Dictionary mapping source names to number of tasks created
        """
        results = {}

        for source_name, source in self.sources.items():
            if not self._should_poll(source_name):
                continue

            tasks_created = await self.poll_source(source_name, source)
            results[source_name] = tasks_created

        return results

    async def run_continuous(self):
        """Run continuous polling loop."""
        self.running = True
        self.logger.info("Starting continuous polling...")

        while self.running:
            try:
                results = await self.run_once()

                # Log summary if any tasks were created
                total = sum(results.values())
                if total > 0:
                    summary = ", ".join(f"{k}={v}" for k, v in results.items() if v > 0)
                    self.logger.info(f"Created {total} tasks: {summary}")

                # Sleep for shortest poll interval
                await asyncio.sleep(10)  # Check every 10 seconds

            except KeyboardInterrupt:
                self.logger.info("Received interrupt, stopping...")
                break
            except Exception as e:
                self.logger.error(f"Error in polling loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait before retrying

        self.running = False
        self.logger.info("Stopped continuous polling")

    def stop(self):
        """Stop the orchestrator."""
        self.running = False


async def main():
    """Main entry point for orchestrator."""
    import argparse

    parser = argparse.ArgumentParser(description="Input sources orchestrator")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to configuration file (YAML)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit instead of continuous polling",
    )
    args = parser.parse_args()

    # Load configuration
    if args.config and args.config.exists():
        config = InputSourcesConfig.from_yaml(args.config)
    else:
        # Use default configuration
        config = InputSourcesConfig()

    # Create orchestrator
    orchestrator = InputSourceOrchestrator(config)

    try:
        if args.once:
            # Run once and exit
            results = await orchestrator.run_once()
            total = sum(results.values())
            print(f"Created {total} tasks")
            if total > 0:
                for source, count in results.items():
                    if count > 0:
                        print(f"  {source}: {count}")
        else:
            # Run continuously
            await orchestrator.run_continuous()
    except KeyboardInterrupt:
        orchestrator.stop()
        print("\nStopped")


if __name__ == "__main__":
    asyncio.run(main())
