# Communication Utilities

Shared utilities for cross-platform communication systems (Email, Twitter, Discord, etc.).

## Overview

This package provides common patterns and utilities for building communication systems:

- **Rate Limiting**: Token bucket algorithm for respecting API limits
- **Message Headers**: Universal message IDs and conversation threading
- **Authentication**: OAuth flows and token management
- **State Management**: File locks and conversation tracking
- **Logging**: Consistent logging across platforms
- **Metrics**: Performance monitoring and error tracking
- **Error Handling**: Retry logic with exponential backoff

## Installation

```python
# Add communication_utils to Python path
import sys
sys.path.insert(0, "/path/to/workspace")

from communication_utils import ...
```

## Usage Examples

### Rate Limiting

```python
from communication_utils.rate_limiting import RateLimiter, GlobalRateLimiter

# Single platform rate limiter
limiter = RateLimiter.for_platform("twitter")  # 300 req/15min

if limiter.can_proceed():
    # Make API call
    pass
else:
    wait_time = limiter.time_until_ready()
    print(f"Wait {wait_time}s before next request")

# Global rate limiter for multiple platforms
global_limiter = GlobalRateLimiter()
global_limiter.add_limiter("twitter", RateLimiter.for_platform("twitter"))
global_limiter.add_limiter("email", RateLimiter.for_platform("email"))

if global_limiter.can_proceed("twitter"):
    # Make Twitter API call
    pass
```

### Message Headers

```python
from communication_utils.messaging import MessageHeaders

# Create new message with universal ID
headers = MessageHeaders.create(
    subject="Re: Previous discussion",
    in_reply_to="<message-id-123>",
    platform="email"
)

# Serialize for storage
headers_dict = headers.to_dict()

# Parse from email
email_content = "Subject: Test\nMessage-ID: <123>\n\nBody..."
parsed = MessageHeaders.parse_headers(email_content)
```

### Authentication

```python
from communication_utils.auth import TokenManager, OAuthManager

# Token management
token = TokenManager.get_token("twitter")
if token:
    headers = TokenManager.create_bearer_header(token)
    # Use headers in API request

# OAuth flow
oauth = OAuthManager.for_twitter(client_id, client_secret)

# Step 1: Get authorization URL
auth_url = oauth.get_authorization_url(state="random_state")
print(f"Visit: {auth_url}")

# Step 2: Exchange code for token
token_info, error = oauth.exchange_code_for_token(auth_code)
if token_info:
    print(f"Access token: {token_info.token}")

# Step 3: Refresh expired token
if token_info.is_expired():
    new_token, error = oauth.refresh_token(token_info.refresh_token)
```

### State Management

```python
from communication_utils.state import FileLock, ConversationTracker, MessageState

# File locking
with FileLock("/tmp/mylock.lock").locked():
    # Critical section - only one process at a time
    update_shared_resource()

# Conversation tracking
tracker = ConversationTracker("/path/to/state")

# Track new message
tracker.track_message(
    conversation_id="conv-123",
    message_id="msg-456",
    in_reply_to="msg-455"
)

# Update message state
tracker.set_message_state("conv-123", "msg-456", MessageState.IN_PROGRESS)
tracker.set_message_state("conv-123", "msg-456", MessageState.COMPLETED)

# Get pending messages
pending = tracker.get_pending_messages("conv-123")
```

### Logging and Monitoring

```python
from communication_utils.monitoring import (
    get_logger,
    configure_logging,
    MetricsCollector,
)

# Configure logging
configure_logging(level="INFO", log_file="/path/to/app.log")

# Get platform-specific logger
logger = get_logger("myapp", "twitter")
logger.info("Processing tweet", tweet_id=123, user="@handle")
logger.error("API error", status_code=429, retry_after=60)

# Collect metrics
metrics = MetricsCollector()

# Track operation
op = metrics.start_operation("send_tweet", "twitter")
try:
    send_tweet_api_call()
    op.complete(success=True)
except Exception as e:
    op.complete(success=False, error=str(e))

# Get statistics
stats = metrics.get_stats(platform="twitter")
print(f"Success rate: {stats['success_rate']}%")
print(f"Average duration: {stats['avg_duration']}s")

# Get operation breakdown
breakdown = metrics.get_operation_breakdown()
for operation, stats in breakdown.items():
    print(f"{operation}: {stats['successful']}/{stats['total']} successful")
```

### Error Handling

```python
from communication_utils.error_handling import (
    retry,
    retry_with_rate_limit,
    RetryConfig,
    RateLimitError,
)

# Simple retry decorator
@retry(max_attempts=3, initial_delay=1.0)
def api_call():
    response = requests.get("https://api.example.com/data")
    response.raise_for_status()
    return response.json()

# Retry with custom configuration
config = RetryConfig(
    max_attempts=5,
    initial_delay=2.0,
    max_delay=60.0,
    exponential_base=2.0,
    jitter=True,
)

@retry(config=config)
def complex_api_call():
    # Will retry up to 5 times with exponential backoff
    pass

# Rate limit aware retry
@retry_with_rate_limit(max_attempts=5)
def rate_limited_api():
    response = requests.get("https://api.example.com/limited")
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", 60))
        raise RateLimitError("twitter", retry_after=retry_after)
    return response.json()
```

## Implementation Status

### ✅ Phase 1: Shared Utilities (Complete)

All core utilities implemented and ready for use:

- [x] Package structure
- [x] Rate limiting (token bucket algorithm)
- [x] Message headers (universal IDs and threading)
- [x] Authentication (OAuth + token management)
- [x] State management (file locks + conversation tracking)
- [x] Logging/monitoring (structured logging + metrics)
- [x] Error handling (retry logic + exponential backoff)

### 🔄 Phase 2: Platform Refactoring (Not Started)

- [ ] Refactor email system to use shared utilities
- [ ] Refactor Twitter system to use shared utilities
- [ ] Refactor Discord system to use shared utilities
- [ ] Remove duplicated code from platform implementations

### 🔄 Phase 3: Cross-Platform Features (Not Started)

- [ ] Unified conversation threading across platforms
- [ ] Cross-platform rate limiting coordination
- [ ] Shared state tracking for conversations
- [ ] Common monitoring dashboard

## Architecture

The communication utilities follow a modular architecture with clear separation of concerns:

```text
communication_utils/
├── rate_limiting/     # Token bucket rate limiting
│   └── limiters.py    # Platform-specific rate limiters
├── messaging/         # Message format and headers
│   └── headers.py     # Universal message IDs, threading
├── auth/              # Authentication and tokens
│   ├── tokens.py      # Token management, validation
│   └── oauth.py       # OAuth 2.0 flows
├── state/             # State management
│   ├── locks.py       # File-based locking
│   └── tracking.py    # Conversation and message tracking
├── monitoring/        # Logging and metrics
│   ├── loggers.py     # Structured logging
│   └── metrics.py     # Performance tracking
└── error_handling/    # Error handling and retry
    ├── errors.py      # Common error classes
    └── retry.py       # Retry decorators with backoff
```

## Design Principles

1. **Platform Agnostic**: Utilities work across email, Twitter, Discord, etc.
2. **Thread Safe**: File locks and state management handle concurrent access
3. **Observable**: Structured logging and metrics for monitoring
4. **Resilient**: Retry logic with exponential backoff for transient failures
5. **Type Safe**: Full type annotations with mypy validation

## Contributing

When adding new utilities:
1. Follow existing patterns (dataclasses, type hints, documentation)
2. Add usage examples to this README
3. Include type annotations for mypy validation
4. Add tests for new functionality
5. Update implementation status section
