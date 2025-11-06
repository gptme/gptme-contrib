# Unified Monitoring Integration Guide

## Overview

This guide demonstrates how to integrate Phase 1 monitoring utilities (PlatformLogger, MetricsCollector) across email, twitter, and discord platforms for consistent logging and metrics collection.

## Quick Start

### Basic Logger Setup

```python
from communication_utils.monitoring import get_logger

# Create platform-specific logger
logger = get_logger("system", "email", log_dir=Path("logs/"))

# Use structured logging with context
logger.info("Processing message", message_id="msg_123", user="user@example.com")
logger.warning("Rate limit approaching", remaining=10, limit=60)
logger.error("Failed to send", error="Connection timeout", retry_count=3)
```

### Basic Metrics Collection

```python
from communication_utils.monitoring import MetricsCollector

# Create collector
metrics = MetricsCollector()

# Track operation
op = metrics.start_operation("send_email", "email")
try:
    send_email_implementation()
    op.complete(success=True)
except Exception as e:
    op.complete(success=False, error=str(e))

# Get statistics
stats = metrics.get_stats()
print(f"Success rate: {stats['success_rate']}%")
print(f"Avg duration: {stats['avg_duration']}s")
```

## Platform Integration Examples

### Email System Integration

**Location**: `gptme-contrib/scripts/email/lib.py`

```python
from communication_utils.monitoring import get_logger, MetricsCollector

class EmailSystem:
    def __init__(self):
        # Setup logging
        self.logger = get_logger("email_system", "email",
                                log_dir=Path("logs/email/"))

        # Setup metrics
        self.metrics = MetricsCollector()

    def send_reply(self, message_id: str, content: str):
        """Send email reply with monitoring."""
        # Start metrics tracking
        op = self.metrics.start_operation("send_reply", "email")

        try:
            # Log operation start
            self.logger.info("Sending reply",
                           message_id=message_id,
                           content_length=len(content))

            # Perform send operation
            send_email_via_smtp(message_id, content)

            # Log success
            self.logger.info("Reply sent successfully",
                           message_id=message_id)
            op.complete(success=True)

        except Exception as e:
            # Log error
            self.logger.error("Failed to send reply",
                            message_id=message_id,
                            error=str(e))
            op.complete(success=False, error=str(e))
            raise

    def get_performance_stats(self) -> dict:
        """Get email system performance statistics."""
        return self.metrics.get_stats("email")
```

### Twitter System Integration

**Location**: `gptme-contrib/scripts/twitter/twitter.py`

```python
from communication_utils.monitoring import get_logger, MetricsCollector

class TwitterSystem:
    def __init__(self):
        # Setup logging
        self.logger = get_logger("twitter_system", "twitter",
                                log_dir=Path("logs/twitter/"))

        # Setup metrics
        self.metrics = MetricsCollector()

    def post_tweet(self, content: str) -> str:
        """Post tweet with monitoring."""
        # Start metrics tracking
        op = self.metrics.start_operation("post_tweet", "twitter")

        try:
            # Log operation start
            self.logger.info("Posting tweet",
                           content_length=len(content))

            # Post tweet via API
            tweet_id = twitter_api_post(content)

            # Log success
            self.logger.info("Tweet posted",
                           tweet_id=tweet_id)
            op.complete(success=True)
            return tweet_id

        except Exception as e:
            # Log error
            self.logger.error("Failed to post tweet",
                            error=str(e))
            op.complete(success=False, error=str(e))
            raise

    def get_performance_stats(self) -> dict:
        """Get twitter system performance statistics."""
        return self.metrics.get_stats("twitter")
```

### Discord System Integration

**Location**: `gptme-contrib/scripts/discord/discord_bot.py`

```python
from communication_utils.monitoring import get_logger, MetricsCollector

class DiscordBot:
    def __init__(self):
        # Setup logging
        self.logger = get_logger("discord_bot", "discord",
                                log_dir=Path("logs/discord/"))

        # Setup metrics
        self.metrics = MetricsCollector()

    async def handle_message(self, message):
        """Handle Discord message with monitoring."""
        # Start metrics tracking
        op = self.metrics.start_operation("handle_message", "discord")

        try:
            # Log operation start
            self.logger.info("Processing message",
                           user=str(message.author),
                           channel=str(message.channel))

            # Process message
            response = await process_discord_message(message)
            await message.channel.send(response)

            # Log success
            self.logger.info("Message processed",
                           user=str(message.author))
            op.complete(success=True)

        except Exception as e:
            # Log error
            self.logger.error("Failed to process message",
                            user=str(message.author),
                            error=str(e))
            op.complete(success=False, error=str(e))
            raise

    def get_performance_stats(self) -> dict:
        """Get discord bot performance statistics."""
        return self.metrics.get_stats("discord")
```

## Monitoring CLI Tool

Create a simple CLI tool to view aggregated metrics across all platforms:

**Location**: `gptme-contrib/scripts/monitoring/view_metrics.py`

```python
#!/usr/bin/env python3
"""
View unified metrics across communication platforms.
"""

import argparse
from pathlib import Path
from communication_utils.monitoring import MetricsCollector

# Import platform systems
from email.lib import EmailSystem
from twitter.twitter import TwitterSystem
from discord.discord_bot import DiscordBot


def display_platform_stats(platform: str, stats: dict):
    """Display statistics for a single platform."""
    print(f"\n{platform.upper()} Statistics:")
    print(f"  Total operations: {stats['total_operations']}")
    print(f"  Successful: {stats['successful_operations']}")
    print(f"  Failed: {stats['failed_operations']}")
    print(f"  Success rate: {stats['success_rate']}%")
    print(f"  Avg duration: {stats['avg_duration']}s")


def main():
    parser = argparse.ArgumentParser(description="View communication platform metrics")
    parser.add_argument("--platform", choices=["email", "twitter", "discord", "all"],
                       default="all", help="Platform to view metrics for")
    parser.add_argument("--errors", action="store_true",
                       help="Show recent errors")
    args = parser.parse_args()

    # Get metrics from each platform
    platforms = {
        "email": EmailSystem(),
        "twitter": TwitterSystem(),
        "discord": DiscordBot()
    }

    if args.platform == "all":
        for name, system in platforms.items():
            stats = system.get_performance_stats()
            display_platform_stats(name, stats)
    else:
        system = platforms[args.platform]
        stats = system.get_performance_stats()
        display_platform_stats(args.platform, stats)

    # Show recent errors if requested
    if args.errors:
        print("\nRecent Errors:")
        for name, system in platforms.items():
            if args.platform in (name, "all"):
                errors = system.metrics.get_recent_errors(limit=5, platform=name)
                if errors:
                    print(f"\n{name.upper()}:")
                    for err in errors:
                        print(f"  [{err['timestamp']}] {err['operation']}: {err['error']}")


if __name__ == "__main__":
    main()
```

## Best Practices

### 1. Always Use Try-Catch with Metrics

```python
op = metrics.start_operation("operation_name", "platform")
try:
    perform_operation()
    op.complete(success=True)
except Exception as e:
    op.complete(success=False, error=str(e))
    raise  # Re-raise after logging
```

### 2. Log at Appropriate Levels

```python
# Info: Normal operations
logger.info("Message received", message_id="123")

# Warning: Unusual but handled situations
logger.warning("Approaching rate limit", remaining=5)

# Error: Failures requiring attention
logger.error("Failed to connect", error=str(e))

# Debug: Detailed troubleshooting info
logger.debug("Parsing headers", raw_headers=headers)
```

### 3. Include Relevant Context

```python
# Good: Structured context
logger.info("Processing request",
           user_id="user_123",
           operation="send_email",
           content_size=len(content))

# Bad: Unstructured string
logger.info(f"Processing request for user_123 to send email of size {len(content)}")
```

### 4. Track All Critical Operations

Track user-facing operations that matter for performance monitoring.

### 5. Periodic Stats Review

```python
# In long-running services, periodically log stats
import time

while running:
    handle_requests()

    # Every hour, log performance stats
    if time.time() - last_stats_time > 3600:
        stats = metrics.get_stats()
        logger.info("Hourly performance report",
                   **stats)
        last_stats_time = time.time()
```

## Configuration

### Log Levels

Set logging level via environment variable:

```bash
export LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR
```

### Log Directory

Configure log directory per platform:

```python
from pathlib import Path

# Separate logs by platform
email_logger = get_logger("email", "email", log_dir=Path("logs/email/"))
twitter_logger = get_logger("twitter", "twitter", log_dir=Path("logs/twitter/"))
discord_logger = get_logger("discord", "discord", log_dir=Path("logs/discord/"))
```

### Metrics Persistence (Optional)

For long-running services, persist metrics periodically:

```python
import json
from pathlib import Path

def save_metrics(metrics: MetricsCollector, path: Path):
    """Save metrics to JSON file."""
    stats = metrics.get_stats()
    breakdown = metrics.get_operation_breakdown()
    errors = metrics.get_recent_errors()

    data = {
        "overall_stats": stats,
        "operation_breakdown": breakdown,
        "recent_errors": errors
    }

    path.write_text(json.dumps(data, indent=2))

# Save every hour
save_metrics(metrics, Path("logs/metrics.json"))
```

## Testing

### Mock Logger for Tests

```python
from unittest.mock import Mock

def test_email_send():
    # Create mock logger
    mock_logger = Mock()

    # Inject into system
    email_system = EmailSystem()
    email_system.logger = mock_logger

    # Test operation
    email_system.send_reply("msg_123", "Test reply")

    # Verify logging
    mock_logger.info.assert_called_with("Sending reply",
                                       message_id="msg_123",
                                       content_length=10)
```

### Verify Metrics Collection

```python
def test_metrics_collection():
    metrics = MetricsCollector()

    # Track operation
    op = metrics.start_operation("test_op", "test_platform")
    op.complete(success=True)

    # Verify stats
    stats = metrics.get_stats()
    assert stats["total_operations"] == 1
    assert stats["successful_operations"] == 1
    assert stats["success_rate"] == 100.0
```

## Migration Strategy

### Phase 1: Add Logging

1. Import logger utilities
2. Create platform logger
3. Add logger calls to existing code
4. No functional changes yet

### Phase 2: Add Metrics

1. Import metrics utilities
2. Create metrics collector
3. Wrap operations with metrics tracking
4. Verify metrics collection

### Phase 3: Enable Monitoring

1. Deploy with monitoring enabled
2. Configure log levels
3. Monitor performance stats
4. Use metrics for optimization

## Related

- Phase 1: Shared utilities implementation (Sessions 492-493)
- Phase 2: Platform integration (Sessions 494, 670, 671)
- Phase 3.1: Cross-platform foundation (Session 856)
- Phase 3.3: Shared configuration (Session 859)
