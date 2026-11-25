"""Tests for the daily-summary prompt size budget.

A fanout day can hold hundreds of journal files (600 files / 2.4 MB on
2026-11-24), which pushed the assembled prompt past the model's 1M-token
ceiling and made every scheduled run fail. These tests pin the bound.
"""

from pathlib import Path

from gptme_activity_summary import cc_backend
from gptme_activity_summary.cc_backend import (
    _bound_journal_entries,
    _truncate_block,
    summarize_daily_with_cc,
)


def _entries(count: int, size: int) -> list[tuple[Path, str]]:
    return [(Path(f"journal/2026-11-24/session-{i:04d}.md"), "x" * size) for i in range(count)]


def test_small_day_is_not_truncated():
    entries = _entries(5, 1_000)
    content, truncated = _bound_journal_entries(entries, 100_000)
    assert truncated == 0
    assert "[... truncated" not in content
    for path, body in entries:
        assert f"### Entry: {path.name}" in content
        assert body in content


def test_large_day_is_bounded_and_keeps_every_entry():
    # Mirrors the real failure shape: 600 entries, ~2.4 MB total.
    entries = _entries(600, 4_000)
    limit = 300_000
    content, truncated = _bound_journal_entries(entries, limit)
    assert len(content) <= limit
    assert truncated == 600
    # Every entry is still represented, so the summary covers the whole day.
    for path, _ in entries:
        assert f"### Entry: {path.name}" in content
    assert "[... truncated" in content


def test_small_entries_keep_full_text_when_large_ones_are_truncated():
    entries = [
        (Path("journal/a.md"), "a" * 500),
        (Path("journal/b.md"), "b" * 50_000),
        (Path("journal/c.md"), "c" * 50_000),
    ]
    content, truncated = _bound_journal_entries(entries, 20_000)
    assert truncated == 2
    # The small entry is cheap, so water-filling keeps it whole.
    assert "a" * 500 in content


def test_truncate_block_keeps_head_and_tail():
    text = "HEAD" + ("m" * 1_000) + "TAIL"
    out = _truncate_block(text, 200)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "[... truncated" in out
    assert len(out) <= 200


def test_daily_summary_prompt_stays_under_budget(monkeypatch):
    captured: dict[str, str] = {}

    def fake_call(prompt: str, **kwargs):
        captured["prompt"] = prompt
        return '{"narrative": "ok"}'

    monkeypatch.setattr(cc_backend, "call_claude_code", fake_call)
    entries = _entries(600, 4_000)
    extra_context = "g" * 500_000
    summarize_daily_with_cc(entries, "2026-11-24", extra_context=extra_context)

    prompt = captured["prompt"]
    # Journal budget + context budget + a small template allowance.
    assert len(prompt) <= (
        cc_backend._MAX_JOURNAL_PROMPT_CHARS + cc_backend._MAX_EXTRA_CONTEXT_CHARS + 5_000
    )
    assert "oversized entries are truncated" in prompt
