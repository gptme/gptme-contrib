# gptme-backoff

Retry with exponential backoff and jitter for flaky API calls, file I/O and
agent tool calls — plus an error classifier that picks a retry strategy per
error type (retry 429s patiently, fail fast on 401s).

**Status:** alpha (`0.1.0`). Small, fully tested API; not yet widely used
inside gptme or other contrib packages.

Two layers:

- **Retry decorators** (`retry.py`) — thin wrappers over
  [tenacity](https://tenacity.readthedocs.io/) with presets for API calls and
  file I/O.
- **Error-type classification** (`error_classification.py`) — maps each
  exception to a retry strategy (attempts, backoff, jitter). Pure standard
  library; does not use tenacity.

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-backoff"
```

The only runtime dependency is `tenacity>=9.0`. In a gptme-contrib checkout it
is installed with `uv sync --all-packages`.

## Quick start

```python
from gptme_backoff import retry_api_call, retry_classified

# Fixed policy: up to 5 attempts, give up after 30 s
@retry_api_call(max_attempts=5, timeout=30)
def call_api() -> dict:
    ...

# Policy chosen per error: 429 -> many patient retries, 401 -> no retry
@retry_classified()
def call_api_classified() -> dict:
    ...
```

`retry_api_call`, `retry_file_op` and `retry_classified` detect whether the
wrapped function is sync or `async def`. The low-level `retry_sync` and
`retry_async` are for one kind each.

## Retry decorators

### `retry_api_call`

Preset for HTTP API calls: exponential backoff, optional jitter, optional
wall-clock timeout.

| Parameter      | Default     | Description |
|----------------|-------------|-------------|
| `max_attempts` | 3           | Max attempts |
| `timeout`      | `None`      | Wall-clock timeout (seconds) |
| `min_wait`     | 1.0         | Minimum wait (seconds) |
| `max_wait`     | 60.0        | Maximum wait (seconds) |
| `multiplier`   | 2.0         | Exponential backoff factor |
| `jitter`       | `True`      | Add random jitter to waits |
| `retry_on`     | `Exception` | Exception type(s) that trigger a retry |

### `retry_file_op`

Preset for file I/O. Retries only `OSError` by default (EAGAIN, EBUSY, …), not
`ValueError` / `TypeError`.

| Parameter      | Default   | Description |
|----------------|-----------|-------------|
| `max_attempts` | 3         | Max attempts |
| `min_wait`     | 0.1       | Minimum wait (seconds) |
| `max_wait`     | 5.0       | Maximum wait (seconds) |
| `multiplier`   | 2.0       | Exponential backoff factor |
| `retry_on`     | `OSError` | Exception type(s) that trigger a retry |

### `retry_sync` / `retry_async`

Low-level decorators over `tenacity.Retrying` / `tenacity.AsyncRetrying`. Pass
any tenacity `stop`, `wait` or `retry` condition (defaults: 3 attempts,
exponential backoff 0.5–30 s ×1.5, retry on any `Exception`, re-raise the last
exception):

```python
import tenacity
from gptme_backoff import retry_sync

@retry_sync(stop=tenacity.stop_after_attempt(5), wait=tenacity.wait_fixed(1))
def fetch(url: str) -> str:
    ...
```

## Error-type classification

### `retry_classified`

```python
from gptme_backoff import retry_classified

def log_retry(exc, strategy, next_attempt, wait):
    print(f"{strategy.value}: retrying (attempt {next_attempt}) in {wait:.1f}s: {exc}")

@retry_classified(on_retry=log_retry)
async def call_api_async() -> dict:
    ...
```

Keyword arguments:

- `classifier` — an `ErrorClassifier` (default: `ErrorClassifier.default()`).
- `configs` — per-strategy `StrategyConfig` overrides, merged over
  `DEFAULT_STRATEGY_CONFIGS`.
- `on_retry` — callback `(exc, strategy, next_attempt, wait)` called before
  each backoff sleep.
- `sleep` — injectable sleep for sync functions (useful in tests; async
  functions always use `asyncio.sleep`).

The last exception is re-raised once the strategy's `max_attempts` is reached.

### Strategies

`max_attempts` counts the first try, so `1` means no retry.

| Strategy      | Max attempts | Base wait | Max wait | Jitter | Default rules map here |
|---------------|--------------|-----------|----------|--------|------------------------|
| `TRANSIENT`   | 4            | 0.5 s     | 30 s     | yes    | 5xx, 408, 425, `ConnectionError`, `TimeoutError` |
| `RATE_LIMIT`  | 6            | 2.0 s     | 120 s    | yes    | 429 |
| `AUTH`        | 1            | —         | —        | no     | other 4xx (401, 403, 404, …) — fail fast |
| `CONSISTENCY` | 8            | 1.0 s     | 60 s     | yes    | nothing by default; register your own (e.g. "not found yet") |
| `UNKNOWN`     | 2            | 1.0 s     | 10 s     | yes    | anything unmatched |

Status codes are read from `exc.status_code`, `exc.status`, `exc.code` or
`exc.response.status_code`, so exceptions from requests, httpx and most SDKs
work without custom rules. Jitter is additive: each wait is spread over
`[wait, 2 × wait)`.

### Custom rules

Rules are checked in order and the first match wins. Start from the defaults
and prepend specific rules:

```python
from gptme_backoff import ErrorClassifier, RetryStrategy, retry_classified

classifier = ErrorClassifier.default()
classifier.register(RetryStrategy.CONSISTENCY, exc_types=[KeyError])
classifier.register(
    RetryStrategy.RATE_LIMIT,
    predicate=lambda e: "rate limit" in str(e).lower(),
)

@retry_classified(classifier=classifier)
def read_eventually_consistent() -> dict:
    ...
```

Or build a classifier from scratch with
`ErrorClassifier(rules=[ErrorRule(strategy, exc_types=(...,)), ...])`; set
`default_strategy` to change what unmatched errors get (default `UNKNOWN`).

## API summary

Exported from `gptme_backoff`: `retry_api_call`, `retry_file_op`,
`retry_sync`, `retry_async`, `retry_classified`, `ErrorClassifier`,
`ErrorRule`, `RetryStrategy`, `StrategyConfig`, `DEFAULT_STRATEGY_CONFIGS`.

## Development

```bash
make test       # pytest
make typecheck  # mypy
```

## License

Same as the gptme-contrib workspace (MIT).
