# gptme-backoff

Retry decorators and error-type classification for resilient agent tool calls.

Built on [tenacity](https://tenacity.readthedocs.io/). Two layers:

- **Phase 1** (`retry.py`): Exponential backoff + jitter decorators with preset
  configurations for API calls and file I/O.
- **Phase 2** (`error_classification.py`): Error-type classification that selects
  retry strategy (attempts, backoff, jitter) per exception class — 429s get
  more attempts + wider jitter; 401s fail immediately.

Both layers are self-contained standard-library code. Phase 2 does not import
tenacity at all — use it standalone or pair it with Phase 1's presets.

---

## Quick Start

```python
from gptme_backoff import retry_api_call

@retry_api_call(max_attempts=5, timeout=30)
def call_api() -> dict:
    ...
```

---

## Phase 1: Retry Decorators

### `retry_sync` / `retry_async`

Low-level decorators wrapping `tenacity.Retrying` / `tenacity.AsyncRetrying`.
Pass any tenacity stop, wait, or retry condition directly:

```python
from gptme_backoff import retry_sync
import tenacity

@retry_sync(stop=tenacity.stop_after_attempt(5), wait=tenacity.wait_fixed(1))
def fetch(url: str) -> str:
    ...
```

### `retry_api_call`

Keyword-argument preset tuned for HTTP API calls. Combines exponential backoff
with optional additive jitter and wall-clock timeout:

```python
from gptme_backoff import retry_api_call

@retry_api_call(max_attempts=5, timeout=30)
def call_api() -> dict:
    ...
```

| Parameter     | Default | Description |
|---------------|---------|-------------|
| `max_attempts`| 3       | Max retry attempts |
| `timeout`     | None    | Wall-clock timeout (seconds) |
| `min_wait`    | 1.0     | Minimum wait (seconds) |
| `max_wait`    | 60.0    | Maximum wait (seconds) |
| `multiplier`  | 2.0     | Exponential backoff factor |
| `jitter`      | True    | Add random jitter to waits |
| `retry_on`    | `Exception` | Which exception types trigger retry |

Works with both sync and async functions (tenacity auto-detects).

### `retry_file_op`

Preset for file I/O. Only retries on `OSError` by default (EAGAIN, EBUSY,
ENOSPC — not `ValueError` or `TypeError`):

```python
from gptme_backoff import retry_file_op

@retry_file_op(max_attempts=5)
def read_config() -> str:
    ...
```

| Parameter     | Default    | Description |
|---------------|------------|-------------|
| `max_attempts`| 3          | Max retry attempts |
| `min_wait`    | 0.1        | Minimum wait (seconds) |
| `max_wait`    | 5.0        | Maximum wait (seconds) |
| `multiplier`  | 2.0        | Exponential backoff factor |
| `retry_on`    | `OSError`  | Which exception types trigger retry |

---

## Phase 2: Error-Type Classification

Phase 2 adds a classification layer on top of (or as an alternative to) the
Phase 1 presets. Instead of one strategy per decorator, the classifier picks a
strategy per *exception type*.

### `ErrorClassifier`

Maps exception types to retry strategies via ordered rules:

```python
from gptme_backoff import ErrorClassifier, RetryStrategy, retry_classified

classifier = ErrorClassifier.default()

# 429 -> RATE_LIMIT (more attempts, wider jitter)
# 401 -> AUTH (no retry)
# ConnectionError -> TRANSIENT (standard backoff)

@retry_classified(classifier=classifier)
def call_api() -> dict:
    ...
```

### Strategies

| Strategy       | Max attempts | Base wait | Max wait | Jitter | Use case |
|----------------|-------------|-----------|----------|--------|----------|
| `TRANSIENT`    | 4           | 0.5s      | 30s      | Yes    | Connection resets, 503s |
| `RATE_LIMIT`   | 6           | 2.0s      | 120s     | Yes    | 429 throttling |
| `AUTH`         | 1           | —         | —        | No     | 401/403 — fail fast |
| `CONSISTENCY`  | 8           | 1.0s      | 60s      | Yes    | Eventual-consistency reads |
| `UNKNOWN`      | 2           | 1.0s      | 10s      | Yes    | Catch-all |

### Custom Rules

Register explicit exception-type or predicate-based rules:

```python
from gptme_backoff import ErrorClassifier, ErrorRule, RetryStrategy

classifier = ErrorClassifier(rules=[
    ErrorRule(RetryStrategy.RATE_LIMIT, exc_types=(RateLimitError,)),
    ErrorRule(RetryStrategy.AUTH, exc_types=(AuthError,)),
    ErrorRule(RetryStrategy.TRANSIENT, exc_types=(ConnectionError, TimeoutError)),
    # Predicate: match any exception with a 429 status code
    ErrorRule(RetryStrategy.RATE_LIMIT, predicate=lambda e: getattr(e, "status_code", 0) == 429),
    # Catch-all
    ErrorRule(RetryStrategy.TRANSIENT, exc_types=(Exception,)),
])
```

The first matching rule wins — specific rules shadow broad ones.

### `retry_classified`

Decorator that runs the classifier loop manually (no tenacity dependency).
Callable for both sync and async functions:

```python
from gptme_backoff import retry_classified, ErrorClassifier

classifier = ErrorClassifier.default()

@retry_classified(classifier=classifier)
def call_api() -> dict:
    ...

# Async works too:
@retry_classified(classifier=classifier)
async def call_api_async() -> dict:
    ...

# Override defaults per call:
call_api(classifier=custom_classifier)
```

The decorator supports:
- An `on_retry` callback invoked before each sleep with `(exception, attempt, strategy)`
- Injectable `sleep` function for testing (`lambda secs: ...`)
- Configurable per-strategy configs

---

## Development

```bash
# Run tests
make test

# Type check
make typecheck
```

---

## License

Same as the gptme-contrib workspace.
