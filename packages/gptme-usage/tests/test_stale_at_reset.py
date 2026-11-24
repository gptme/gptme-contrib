from datetime import datetime, timedelta, timezone

from gptme_usage.stale import QuotaReading, summarize, windows_at_reset

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
RESET = T0 + timedelta(days=7)


def reading(
    account: str, at: datetime, util: float, reset_at: datetime
) -> QuotaReading:
    return QuotaReading(account, at, util, (reset_at - at).total_seconds())


def test_newest_reading_before_reset_gives_stale():
    rs = [
        reading("a", T0 + timedelta(days=1), 0.2, RESET),
        # implied reset drifts by a minute: still the same window
        reading("a", RESET - timedelta(hours=2), 0.85, RESET + timedelta(minutes=1)),
    ]
    [w] = windows_at_reset(rs, now=RESET + timedelta(days=1))
    assert w.account == "a"
    assert w.final_utilization == 0.85
    assert w.stale is not None and abs(w.stale - 0.15) < 1e-9


def test_reading_too_long_before_reset_is_unobserved():
    rs = [reading("a", RESET - timedelta(hours=10), 0.5, RESET)]
    [w] = windows_at_reset(rs, now=RESET + timedelta(hours=1))
    assert w.stale is None
    assert not w.observed


def test_running_window_is_skipped():
    rs = [reading("a", T0 + timedelta(days=1), 0.3, RESET)]
    assert windows_at_reset(rs, now=T0 + timedelta(days=2)) == []


def test_accounts_and_resets_are_kept_apart():
    other_reset = RESET + timedelta(days=2)  # different account phase
    next_week = RESET + timedelta(days=7)
    rs = [
        reading("a", RESET - timedelta(hours=1), 0.95, RESET),
        reading("a", next_week - timedelta(hours=1), 0.6, next_week),
        reading("b", other_reset - timedelta(hours=1), 1.2, other_reset),
    ]
    ws = windows_at_reset(rs, now=next_week + timedelta(hours=1))
    assert [(w.account, w.reset_at) for w in ws] == [
        ("a", RESET),
        ("a", next_week),
        ("b", other_reset),
    ]
    # over-limit utilization clamps stale at zero
    assert ws[2].stale == 0.0


def test_negative_resets_in_is_ignored():
    rs = [QuotaReading("a", RESET - timedelta(hours=1), 0.9, -5)]
    assert windows_at_reset(rs, now=RESET + timedelta(days=1)) == []


def test_summary_counts_unobserved_against_coverage_not_target():
    rs = [
        reading("a", RESET - timedelta(hours=1), 0.95, RESET),  # 5% stale
        reading("b", RESET - timedelta(hours=1), 0.70, RESET),  # 30% stale
        reading("c", RESET - timedelta(hours=12), 0.99, RESET),  # unobserved
    ]
    s = summarize(windows_at_reset(rs, now=RESET + timedelta(hours=1)))
    assert (s.windows, s.observed, s.at_or_under_target) == (3, 2, 1)
    assert abs(s.coverage - 2 / 3) < 1e-9
