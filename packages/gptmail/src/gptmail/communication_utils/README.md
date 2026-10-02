# gptmail.communication_utils

Shared building blocks for agent communication channels: rate limiting, file locks,
reply/conversation tracking, cross-platform message headers, OAuth and token helpers,
retry with backoff, and structured logging and metrics.

These utilities ship inside the [`gptmail`](../../../README.md) package. gptmail itself
uses them (`AgentEmail` uses the rate limiter, file locks and `ConversationTracker`;
`gptmail agent` uses the tracker). So do the Discord, Telegram and Twitter scripts in
this repository. Import them as `gptmail.communication_utils.<module>`. For the OAuth
callback server, install `gptmail[oauth]`.

## Modules

```text
communication_utils/
├── rate_limiting/limiters.py   # RateLimiter (token bucket), GlobalRateLimiter
├── state/                      # FileLock, LockError, ConversationTracker, MessageState
├── messaging/                  # MessageHeaders, parse_headers, thread split/join, formatting
├── auth/                       # TokenManager, TokenInfo, OAuthManager, token refresh/storage,
│                               #   CallbackServer (needs gptmail[oauth])
├── monitoring/                 # get_logger, configure_logging, MetricsCollector
├── error_handling/             # retry, RetryConfig, exponential_backoff, error classes
└── outbound_redact.py          # optional secret gate used before sending; blocks on a detected secret, skipped with a warning if redact is absent
```

## Examples

### Rate limiting

```python
from gptmail.communication_utils.rate_limiting.limiters import GlobalRateLimiter, RateLimiter

limiter = RateLimiter.for_platform("twitter")   # presets: email, twitter, discord
if limiter.can_proceed():
    ...                                          # make the call
else:
    print(f"wait {limiter.time_until_ready():.1f}s")

limits = GlobalRateLimiter()                     # creates per-platform limiters on demand
if limits.can_proceed("email"):
    ...
```

### State: locks and conversation tracking

```python
from gptmail.communication_utils.state import ConversationTracker, FileLock, MessageState

with FileLock("/path/to/job.lock", timeout=0).locked():
    ...                                          # only one process at a time

tracker = ConversationTracker("/path/to/state")  # one JSON file per conversation
tracker.track_message("conv-1", "msg-2", in_reply_to="msg-1", channel="email")
tracker.set_message_state("conv-1", "msg-2", MessageState.COMPLETED,
                          metadata={"reply_id": "msg-3"})
pending = tracker.get_pending_messages("conv-1")
done = tracker.get_completed_messages("conv-1")  # COMPLETED + NO_REPLY_NEEDED
```

`MessageState` values: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`,
`NO_REPLY_NEEDED`.

### Message headers

```python
from gptmail.communication_utils.messaging import MessageHeaders, parse_headers

headers = MessageHeaders.create(
    from_address="agent@example.com",
    to_address="you@example.com",
    subject="Re: status",
    platform="email",
    platform_message_id="<abc@example.com>",
    in_reply_to="<prev@example.com>",
)
data = headers.to_dict()
parsed = parse_headers("Subject: Test\nMessage-ID: <123>\n\nBody", platform="email")
```

### Auth and tokens

```python
from gptmail.communication_utils.auth import OAuthManager, TokenManager

token = TokenManager.get_token("github")   # reads GITHUB_TOKEN; also email/twitter/discord
if token:
    headers = TokenManager.create_bearer_header(token)

oauth = OAuthManager.for_twitter(client_id, client_secret)   # also OAuthManager.for_github
url = oauth.get_authorization_url(state="random-state")
token_info, error = oauth.exchange_code_for_token(auth_code)
```

`TokenManager.get_token` reads `GMAIL_APP_PASSWORD`, `TWITTER_BEARER_TOKEN`,
`DISCORD_TOKEN` or `GITHUB_TOKEN`, depending on the platform.

### Logging and metrics

```python
from gptmail.communication_utils.monitoring import MetricsCollector, configure_logging, get_logger

configure_logging(level="INFO")
log = get_logger("mybot", "discord")
log.info("message received", channel_id=123)

metrics = MetricsCollector()
op = metrics.start_operation("send", "discord")
op.complete(success=True)
print(metrics.get_stats(platform="discord")["success_rate"])
```

### Retry

```python
from gptmail.communication_utils.error_handling import RateLimitError, RetryConfig, retry
from gptmail.communication_utils.error_handling.retry import retry_with_rate_limit

@retry(max_attempts=3, initial_delay=1.0)
def call_api(): ...

@retry(config=RetryConfig(max_attempts=5, max_delay=60.0, jitter=True))
def call_slow_api(): ...

@retry_with_rate_limit(max_attempts=5)   # honours RateLimitError.retry_after
def call_limited_api():
    raise RateLimitError("twitter", retry_after=60)
```

## Contributing

Keep utilities platform-agnostic and fully type-annotated, add tests under
`packages/gptmail/tests/`, and update the examples here when you add or change a
public API.
