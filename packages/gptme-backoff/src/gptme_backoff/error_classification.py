"""Error-type classification + per-type retry strategy (Phase 2).

Phase 1 (``retry.py``) gives exponential backoff with jitter and coarse presets
(``retry_api_call``, ``retry_file_op``) where every exception inside a preset
shares one strategy. Phase 2 adds a thin classification layer on top: an
:class:`ErrorClassifier` maps an exception to a :class:`RetryStrategy`, and
:func:`retry_classified` runs a retry loop whose attempts/backoff/jitter are
chosen *per error class*.

Design goals:

- **Self-contained.** Only the standard library is used so the classifier can be
  reused without pulling in optional backoff deps.
- **Extensible.** Callers register ``(exception_type | predicate) -> strategy``
  rules; the first matching rule wins, so specific rules can shadow broad ones.
- **Honest fail-fast.** Auth / 4xx-style errors get ``max_attempts=1`` (no
  retry) — retrying a 401 only wastes time and can trip lockouts.

Example::

    classifier = ErrorClassifier.default()

    @retry_classified(classifier=classifier)
    def call_api():
        ...  # raises RateLimitError on 429, AuthError on 401

    call_api()  # 429 -> jittered exponential backoff; 401 -> immediate raise
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "RetryStrategy",
    "StrategyConfig",
    "ErrorRule",
    "ErrorClassifier",
    "retry_classified",
    "DEFAULT_STRATEGY_CONFIGS",
]


class RetryStrategy(Enum):
    """How a class of error should be retried."""

    TRANSIENT = "transient"
    """Temporary failure (connection reset, 503). Standard backoff."""

    RATE_LIMIT = "rate_limit"
    """Throttled (429). More attempts, wider jitter to de-correlate retries."""

    AUTH = "auth"
    """Authn/authz or other 4xx client errors. Fail fast — do not retry."""

    CONSISTENCY = "consistency"
    """Eventual-consistency / not-found-yet. Longer backoff, more attempts."""

    UNKNOWN = "unknown"
    """Unclassified. Conservative single-shot retry."""


@dataclass(frozen=True)
class StrategyConfig:
    """Per-strategy retry parameters.

    Attempts are total tries including the first. ``max_attempts=1`` means no
    retry. The wait before retry ``n`` (1-indexed) is::

        wait = min(base_wait * multiplier ** (n - 1), max_wait)
        if jitter: wait += random.uniform(0, wait)
    """

    max_attempts: int = 3
    base_wait: float = 1.0
    multiplier: float = 2.0
    max_wait: float = 60.0
    jitter: bool = True

    def compute_wait(self, attempt: int) -> float:
        """Backoff before the given (1-indexed) retry attempt.

        ``attempt=1`` is the wait after the first failure, before the 2nd try.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        wait = self.base_wait * (self.multiplier ** (attempt - 1))
        wait = min(wait, self.max_wait)
        if self.jitter:
            # Additive jitter: spread retries across [wait, 2*wait) so a fleet
            # of callers hitting the same 429 don't all retry in lockstep. This
            # is *not* AWS "full jitter" ([0, wait)) — the spread (variance
            # wait**2/12) is identical, but the higher floor keeps the
            # exponential backoff intact so retries never collapse toward 0.
            wait += random.uniform(0, wait)
        return wait


#: Sensible defaults per strategy. AUTH fails fast; RATE_LIMIT and CONSISTENCY
#: get extra attempts; CONSISTENCY waits longer between tries.
DEFAULT_STRATEGY_CONFIGS: dict[RetryStrategy, StrategyConfig] = {
    RetryStrategy.TRANSIENT: StrategyConfig(
        max_attempts=4, base_wait=0.5, multiplier=2.0, max_wait=30.0, jitter=True
    ),
    RetryStrategy.RATE_LIMIT: StrategyConfig(
        max_attempts=6, base_wait=2.0, multiplier=2.0, max_wait=120.0, jitter=True
    ),
    RetryStrategy.AUTH: StrategyConfig(
        max_attempts=1, base_wait=0.0, multiplier=1.0, max_wait=0.0, jitter=False
    ),
    RetryStrategy.CONSISTENCY: StrategyConfig(
        max_attempts=8, base_wait=1.0, multiplier=1.5, max_wait=60.0, jitter=True
    ),
    RetryStrategy.UNKNOWN: StrategyConfig(
        max_attempts=2, base_wait=1.0, multiplier=2.0, max_wait=10.0, jitter=True
    ),
}


@dataclass(frozen=True)
class ErrorRule:
    """A single classification rule.

    Either ``exc_types`` (matched with ``isinstance``) or ``predicate``
    (an arbitrary ``exc -> bool`` callback) must be set. ``predicate`` is useful
    for matching on a status-code attribute rather than a type, e.g. mapping any
    exception with ``.status_code == 429`` to ``RATE_LIMIT``.
    """

    strategy: RetryStrategy
    exc_types: tuple[type[BaseException], ...] = ()
    predicate: Callable[[BaseException], bool] | None = None

    def matches(self, exc: BaseException) -> bool:
        if self.exc_types and isinstance(exc, self.exc_types):
            return True
        if self.predicate is not None:
            try:
                return bool(self.predicate(exc))
            except Exception:  # a broken predicate must not break classification
                logger.debug(
                    "error-rule predicate raised; treating as no-match", exc_info=True
                )
                return False
        return False


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across common client libraries."""
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    # requests/httpx style: exc.response.status_code
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val
    return None


# 4xx codes that are transient despite being client-class, so they should be
# retried rather than failed fast. 408 Request Timeout (server gave up waiting;
# RFC 7231 §6.5.7 explicitly allows repeating the request) and 425 Too Early
# (replay protection; safe to retry later). Mirrors urllib3/requests' default
# retryable status set, which includes 408 alongside 429/5xx.
_RETRYABLE_4XX = frozenset({408, 425})


def _default_rules() -> list[ErrorRule]:
    """Built-in rules matched in order; first match wins.

    Status-code predicates come first so a typed ``ConnectionError`` that also
    happens to carry a 429 is still treated as a rate-limit.
    """

    def is_rate_limit(exc: BaseException) -> bool:
        return _status_code(exc) == 429

    def is_retryable_client(exc: BaseException) -> bool:
        return _status_code(exc) in _RETRYABLE_4XX

    def is_auth_or_client(exc: BaseException) -> bool:
        code = _status_code(exc)
        # 401/403/404 and other 4xx -> fail fast. 429 (rate-limit) and the
        # transient 4xx in _RETRYABLE_4XX are handled by earlier rules.
        return (
            code is not None
            and 400 <= code < 500
            and code != 429
            and code not in _RETRYABLE_4XX
        )

    def is_server_error(exc: BaseException) -> bool:
        code = _status_code(exc)
        return code is not None and 500 <= code < 600

    return [
        ErrorRule(RetryStrategy.RATE_LIMIT, predicate=is_rate_limit),
        ErrorRule(RetryStrategy.TRANSIENT, predicate=is_retryable_client),
        ErrorRule(RetryStrategy.AUTH, predicate=is_auth_or_client),
        ErrorRule(RetryStrategy.TRANSIENT, predicate=is_server_error),
        ErrorRule(
            RetryStrategy.TRANSIENT,
            exc_types=(ConnectionError, TimeoutError),
        ),
    ]


@dataclass
class ErrorClassifier:
    """Maps exceptions to :class:`RetryStrategy` via ordered rules."""

    rules: list[ErrorRule] = field(default_factory=list)
    default_strategy: RetryStrategy = RetryStrategy.UNKNOWN

    @classmethod
    def default(cls) -> ErrorClassifier:
        """Classifier preloaded with sensible built-in rules."""
        return cls(rules=_default_rules())

    def register(
        self,
        strategy: RetryStrategy,
        *,
        exc_types: Iterable[type[BaseException]] = (),
        predicate: Callable[[BaseException], bool] | None = None,
        prepend: bool = True,
    ) -> ErrorClassifier:
        """Add a rule. ``prepend`` (default) lets specific rules shadow broad ones."""
        types = tuple(exc_types)
        if not types and predicate is None:
            raise ValueError("register() needs exc_types or a predicate")
        rule = ErrorRule(strategy=strategy, exc_types=types, predicate=predicate)
        if prepend:
            self.rules.insert(0, rule)
        else:
            self.rules.append(rule)
        return self

    def classify(self, exc: BaseException) -> RetryStrategy:
        for rule in self.rules:
            if rule.matches(exc):
                return rule.strategy
        return self.default_strategy


def retry_classified(
    *,
    classifier: ErrorClassifier | None = None,
    configs: dict[RetryStrategy, StrategyConfig] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[BaseException, RetryStrategy, int, float], None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that retries a function using a per-error-class strategy.

    Args:
        classifier: maps raised exceptions to a strategy. Defaults to
            :meth:`ErrorClassifier.default`.
        configs: per-strategy parameters. Defaults to
            :data:`DEFAULT_STRATEGY_CONFIGS`.
        sleep: injectable sleep (tests pass a no-op / recorder).
        on_retry: optional callback ``(exc, strategy, next_attempt, wait)``
            invoked before each backoff sleep.

    The wrapped function re-raises the last exception once the strategy's
    ``max_attempts`` is exhausted.
    """
    active_classifier = classifier or ErrorClassifier.default()
    active_configs = {**DEFAULT_STRATEGY_CONFIGS, **(configs or {})}

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        return await func(*args, **kwargs)  # type: ignore[no-any-return]
                    except Exception as exc:
                        strategy = active_classifier.classify(exc)
                        config = active_configs[strategy]
                        if attempt >= config.max_attempts:
                            logger.debug(
                                "retry_classified: giving up on %s after %d attempt(s) [%s]",
                                func.__name__,
                                attempt,
                                strategy.value,
                            )
                            raise
                        wait = config.compute_wait(attempt)
                        logger.debug(
                            "retry_classified: %s failed (attempt %d/%d, %s) -> sleeping %.2fs",
                            func.__name__,
                            attempt,
                            config.max_attempts,
                            strategy.value,
                            wait,
                        )
                        if on_retry is not None:
                            on_retry(exc, strategy, attempt + 1, wait)
                        await asyncio.sleep(wait)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    strategy = active_classifier.classify(exc)
                    config = active_configs[strategy]
                    if attempt >= config.max_attempts:
                        logger.debug(
                            "retry_classified: giving up on %s after %d attempt(s) [%s]",
                            func.__name__,
                            attempt,
                            strategy.value,
                        )
                        raise
                    wait = config.compute_wait(attempt)
                    logger.debug(
                        "retry_classified: %s failed (attempt %d/%d, %s) -> sleeping %.2fs",
                        func.__name__,
                        attempt,
                        config.max_attempts,
                        strategy.value,
                        wait,
                    )
                    if on_retry is not None:
                        on_retry(exc, strategy, attempt + 1, wait)
                    sleep(wait)

        return wrapper

    return decorator
