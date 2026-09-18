"""Wire-contract tests: filenames, timestamp shape, fail-open, never-shorten.

These are the tests both sides of the contract consume (task step 1.6). They pin
the shapes that, if reader and writer disagree, would break an agent silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_block_registry import (
    arm_block_path,
    backend_block_path,
    block_deadline,
    clear_block,
    format_timestamp,
    is_canonical_timestamp,
    limit_window_from_text,
    model_safe,
    openrouter_block_path,
    parse_until,
    pool_block_path,
    read_block_until,
    write_block,
)

UTC = timezone.utc


# -- filenames --------------------------------------------------------------


def test_model_safe_matches_writer_derivation(tmp_path: Path) -> None:
    assert model_safe("openrouter/deepseek/v4.1 flash") == (
        "openrouter-deepseek-v4.1-flash"
    )


def test_arm_block_filenames(tmp_path: Path) -> None:
    assert arm_block_path(tmp_path, "gptme", "glm-5.2", "crash_loop").name == (
        "gptme-glm-5.2-crash-loop-until.txt"
    )
    assert arm_block_path(tmp_path, "gptme", "glm-5.2", "daily_crash_loop").name == (
        "gptme-glm-5.2-daily-crash-loop-until.txt"
    )
    assert arm_block_path(tmp_path, "claude-code", "sonnet", "quality").name == (
        "claude-code-sonnet-quality-blocked-until.txt"
    )


def test_unknown_arm_kind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown arm block kind"):
        arm_block_path(tmp_path, "gptme", "x", "not-a-kind")


def test_backend_pool_and_openrouter_filenames(tmp_path: Path) -> None:
    assert backend_block_path(tmp_path, "copilot-cli").name == (
        "copilot-cli-blocked-until.txt"
    )
    assert pool_block_path(tmp_path, "chatgpt-sub").name == (
        "pool-chatgpt-sub-blocked-until.txt"
    )
    assert openrouter_block_path(tmp_path).name == ("openrouter-daily-limit-until.txt")
    # context is lowercased to match the shell writer's `tr A-Z a-z`
    assert openrouter_block_path(tmp_path, "AUTONOMOUS_DEEPSEEK").name == (
        "openrouter-autonomous_deepseek-daily-limit-until.txt"
    )


# -- timestamps -------------------------------------------------------------


def test_format_timestamp_is_fixed_width_utc() -> None:
    dt = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)
    assert format_timestamp(dt) == "2026-09-19T00:00:00+00:00"
    # non-UTC input is converted, not rejected
    assert format_timestamp(dt.astimezone(timezone(timedelta(hours=2)))) == (
        "2026-09-19T00:00:00+00:00"
    )


def test_parse_until_accepts_z_and_naive() -> None:
    assert parse_until("2026-09-19T00:00:00Z") == datetime(2026, 9, 19, tzinfo=UTC)
    assert parse_until("2026-09-19T00:00:00") == datetime(2026, 9, 19, tzinfo=UTC)
    assert parse_until("  2026-09-19T00:00:00+00:00\n") == datetime(
        2026, 9, 19, tzinfo=UTC
    )


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", "2026-13-45T99:99:99"])
def test_parse_until_garbled_is_none(raw: str) -> None:
    assert parse_until(raw) is None


def test_canonical_timestamp_detection() -> None:
    assert is_canonical_timestamp("2026-09-19T00:00:00+00:00")
    # A trailing newline is tolerated (writers append one).
    assert is_canonical_timestamp("2026-09-19T00:00:00+00:00\n")
    # `Z` and naive/non-fixed-width shapes are NOT canonical: the shell writer
    # only honours the exact `+00:00` shape, so neither side may trust them as
    # a never-shorten floor (they get overwritten instead).
    assert not is_canonical_timestamp("2026-09-19T00:00:00Z")
    assert not is_canonical_timestamp("2026-09-19T00:00:00")
    assert not is_canonical_timestamp("2026-09-19 00:00:00+00:00")
    assert not is_canonical_timestamp("garbage")


def test_lexicographic_ordering_holds_for_canonical_shape() -> None:
    earlier = format_timestamp(datetime(2026, 9, 19, tzinfo=UTC))
    later = format_timestamp(datetime(2026, 9, 20, tzinfo=UTC))
    assert earlier < later


# -- writers ----------------------------------------------------------------


def test_write_block_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "gptme-glm-5.2-crash-loop-until.txt"
    until = datetime(2026, 9, 19, tzinfo=UTC)
    write_block(path, until)
    assert read_block_until(path) == until


def test_write_block_never_shortens(tmp_path: Path) -> None:
    path = tmp_path / "block.txt"
    long_deadline = datetime(2026, 9, 19, tzinfo=UTC)
    write_block(path, long_deadline)
    # a short block arriving on top must not re-open the arm
    write_block(path, datetime(2026, 9, 18, 12, tzinfo=UTC))
    assert read_block_until(path) == long_deadline


def test_write_block_extends(tmp_path: Path) -> None:
    path = tmp_path / "block.txt"
    write_block(path, datetime(2026, 9, 18, 12, tzinfo=UTC))
    longer = datetime(2026, 9, 19, tzinfo=UTC)
    write_block(path, longer)
    assert read_block_until(path) == longer


def test_write_block_overwrites_non_canonical_existing(tmp_path: Path) -> None:
    path = tmp_path / "block.txt"
    path.write_text("2099-01-01 00:00:00\n")  # not the canonical wire shape
    now = datetime(2026, 9, 18, tzinfo=UTC)
    write_block(path, now)
    assert read_block_until(path) == now


def test_write_block_concurrent_writes_never_shorten(tmp_path: Path) -> None:
    """Two racing writers: the longer deadline must always win, regardless of
    which one reads the (empty) existing file first (P1 regression)."""
    import threading

    path = tmp_path / "block.txt"
    short = datetime(2026, 9, 18, 12, tzinfo=UTC)
    long_deadline = datetime(2026, 9, 19, tzinfo=UTC)
    barrier = threading.Barrier(2)

    def write_short() -> None:
        barrier.wait()
        write_block(path, short)

    def write_long() -> None:
        barrier.wait()
        write_block(path, long_deadline)

    for _ in range(20):
        if path.exists():
            path.unlink()
        barrier.reset()
        t1 = threading.Thread(target=write_short)
        t2 = threading.Thread(target=write_long)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert read_block_until(path) == long_deadline


def test_write_block_concurrent_writers_never_observe_partial_file(
    tmp_path: Path,
) -> None:
    """A concurrent reader must always see a complete, parseable timestamp —
    never a truncated/empty file mid-write (P1 regression)."""
    import threading

    path = tmp_path / "block.txt"
    write_block(path, datetime(2026, 9, 18, tzinfo=UTC))
    stop = threading.Event()
    observed_empty = False

    def writer() -> None:
        hour = 0
        while not stop.is_set():
            write_block(path, datetime(2026, 9, 19, hour % 24, tzinfo=UTC))
            hour += 1

    def reader() -> None:
        nonlocal observed_empty
        for _ in range(300):
            if read_block_until(path) is None:
                observed_empty = True

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()
    t_reader.join()
    stop.set()
    t_writer.join()
    assert not observed_empty


def test_clear_block(tmp_path: Path) -> None:
    path = tmp_path / "block.txt"
    write_block(path, datetime(2026, 9, 19, tzinfo=UTC))
    assert clear_block(path) is True
    assert clear_block(path) is False  # idempotent


def test_clear_and_write_race_never_interleaves(tmp_path: Path) -> None:
    """A clear and a write racing on the same file must not interleave: the
    write's temp-file rename must not resurrect a block right after a clear
    removed it, nor can a clear silently drop a write's result mid-flight —
    each op fully completes before the other starts (P1 regression)."""
    import threading

    path = tmp_path / "block.txt"
    deadline = datetime(2026, 9, 19, tzinfo=UTC)
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def do_clear() -> None:
        barrier.wait()
        results.append(clear_block(path))

    def do_write() -> None:
        barrier.wait()
        write_block(path, deadline)

    for _ in range(20):
        write_block(path, deadline)
        results.clear()
        barrier.reset()
        t1 = threading.Thread(target=do_clear)
        t2 = threading.Thread(target=do_write)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # Whichever ran last determines the end state, but it must be one of
        # exactly two consistent outcomes — never a half-written file.
        after = read_block_until(path)
        assert after in (None, deadline)


# -- OpenRouter window math -------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Key limit exceeded (daily limit)", "daily"),
        ("Key limit exceeded (weekly limit)", "weekly"),
        ("Key limit exceeded (monthly limit)", "monthly"),
        # phrase without a window falls back to the historical default
        ("Key limit exceeded", "daily"),
        # credit exhaustion is a distinct 402 error shape, classified separately
        ("Insufficient credits", "credits"),
        ("This request requires more credits, or fewer max_tokens.", "credits"),
        # credits takes precedence when both phrases somehow co-occur
        ("Key limit exceeded (daily limit); insufficient credits", "credits"),
        # unrelated errors are not a limit at all
        ("rate limit exceeded", None),
        ("", None),
    ],
)
def test_limit_window_from_text(text: str, expected: str | None) -> None:
    assert limit_window_from_text(text) == expected


def test_block_deadline_daily_targets_next_utc_midnight() -> None:
    now = datetime(2026, 9, 18, 13, 37, 12, tzinfo=UTC)
    assert block_deadline("daily", now) == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)


def test_block_deadline_weekly_monthly_rolling_24h() -> None:
    now = datetime(2026, 9, 18, 13, 37, 12, tzinfo=UTC)
    assert block_deadline("weekly", now) == now + timedelta(hours=24)
    assert block_deadline("monthly", now) == now + timedelta(hours=24)


def test_block_deadline_credits_4h_and_unknown_defaults_daily() -> None:
    now = datetime(2026, 9, 18, 13, 37, 12, tzinfo=UTC)
    assert block_deadline("credits", now) == now + timedelta(hours=4)
    assert block_deadline("nonsense", now) == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
