"""StrategyConfig.compute_wait overflows instead of capping at max_wait."""

from gptme_backoff.error_classification import (
    ErrorClassifier,
    RetryStrategy,
    StrategyConfig,
    retry_classified,
)


def test_compute_wait_caps_instead_of_overflowing():
    cfg = StrategyConfig(
        max_attempts=5000, base_wait=1.0, multiplier=2.0, max_wait=60.0, jitter=False
    )
    # 2.0 ** 1024 overflows a float; the documented formula is min(..., max_wait)
    assert cfg.compute_wait(1025) == 60.0


def test_long_retry_loop_reraises_original_error_not_overflow():
    class Flaky(Exception):
        pass

    configs = {
        RetryStrategy.UNKNOWN: StrategyConfig(
            max_attempts=1100,
            base_wait=1.0,
            multiplier=2.0,
            max_wait=60.0,
            jitter=False,
        )
    }
    waits = []

    @retry_classified(classifier=ErrorClassifier(), configs=configs, sleep=waits.append)
    def poll():
        raise Flaky("not yet")

    import pytest

    with pytest.raises(Flaky):
        poll()
