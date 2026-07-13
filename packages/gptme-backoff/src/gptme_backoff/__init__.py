"""gptme-backoff — retry decorators and utilities built on tenacity."""

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
)
from .circuit_breaker import (
    State as CircuitBreakerState,
)
from .error_classification import (
    DEFAULT_STRATEGY_CONFIGS,
    ErrorClassifier,
    ErrorRule,
    RetryStrategy,
    StrategyConfig,
    retry_classified,
)
from .retry import retry_api_call, retry_async, retry_file_op, retry_sync

__all__ = [
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "CircuitBreakerState",
    # Retry decorators
    "retry_api_call",
    "retry_async",
    "retry_file_op",
    "retry_sync",
    # Phase 2: error-type classification
    "RetryStrategy",
    "StrategyConfig",
    "ErrorRule",
    "ErrorClassifier",
    "retry_classified",
    "DEFAULT_STRATEGY_CONFIGS",
]
