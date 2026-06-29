"""Tests for summarizer_fired detection in extract_signals (gptme format)."""

from __future__ import annotations

from gptme_sessions.signals import extract_signals

# Keep in sync with tooloutput_trimmer.hooks.trimmer.SUMMARIZATION_MARKER
_SUMMARIZATION_MARKER = "[Summarized previous tool outputs]"


def _msg(role: str, content: str, ts: str = "2026-01-01T10:00:00+00:00") -> dict:
    return {"role": role, "content": content, "timestamp": ts}


def _ts(n: int) -> str:
    return f"2026-01-01T10:00:{n:02d}+00:00"


def test_summarizer_fired_false_when_no_summary() -> None:
    """summarizer_fired is False when no summary marker appears in the session."""
    msgs = [
        _msg("user", "Run ls", _ts(0)),
        _msg("assistant", '@shell(c1): {"cmd": "ls"}', _ts(1)),
        _msg("system", "Ran command: `ls`\nfile1.txt\nfile2.txt", _ts(2)),
    ]
    signals = extract_signals(msgs)
    assert signals["summarizer_fired"] is False


def test_summarizer_fired_true_when_marker_present() -> None:
    """summarizer_fired is True when a system message starts with the marker."""
    summary_content = (
        f"{_SUMMARIZATION_MARKER}\n" "Summary of previous tool calls:\n" "- ls: listed files"
    )
    msgs = [
        _msg("user", "Run ls", _ts(0)),
        _msg("assistant", '@shell(c1): {"cmd": "ls"}', _ts(1)),
        _msg("system", summary_content, _ts(2)),
        _msg("assistant", '@shell(c2): {"cmd": "pwd"}', _ts(3)),
        _msg("system", "Ran command: `pwd`\n/home/bob", _ts(4)),
    ]
    signals = extract_signals(msgs)
    assert signals["summarizer_fired"] is True


def test_summarizer_fired_false_for_empty_session() -> None:
    """summarizer_fired is False for a session with no messages."""
    signals = extract_signals([])
    assert signals["summarizer_fired"] is False


def test_summarizer_fired_false_when_marker_in_non_system_role() -> None:
    """summarizer_fired is False when the marker appears in a non-system role (e.g., user)."""
    msgs = [
        _msg("user", f"{_SUMMARIZATION_MARKER}\nsome content", _ts(0)),
        _msg("assistant", "ok", _ts(1)),
    ]
    signals = extract_signals(msgs)
    assert signals["summarizer_fired"] is False


def test_summarizer_fired_false_when_marker_not_at_start() -> None:
    """summarizer_fired is False when marker appears mid-content (not at start of system msg)."""
    msgs = [
        _msg("user", "Run ls", _ts(0)),
        _msg("assistant", '@shell(c1): {"cmd": "ls"}', _ts(1)),
        _msg(
            "system",
            f"Ran command: `ls`\nsome stuff\n{_SUMMARIZATION_MARKER}",
            _ts(2),
        ),
    ]
    signals = extract_signals(msgs)
    assert signals["summarizer_fired"] is False


def test_summarizer_fired_true_even_with_other_signals() -> None:
    """summarizer_fired is True alongside normal productivity signals (commits, writes)."""
    summary_content = (
        f"{_SUMMARIZATION_MARKER}\n" "Summary of previous tool calls:\n" "- patch: wrote foo.py"
    )
    commit_output = "[master abc1234] feat: add foo (abc1234)"
    msgs = [
        _msg("user", "Add foo.py", _ts(0)),
        _msg("assistant", '@patch(c1): {"path": "foo.py", "content": "x=1"}', _ts(1)),
        _msg("system", summary_content, _ts(2)),
        _msg("assistant", '@shell(c2): {"cmd": "git commit -am feat: add foo"}', _ts(3)),
        _msg("system", f"Ran command: `git commit`\n{commit_output}", _ts(4)),
    ]
    signals = extract_signals(msgs)
    assert signals["summarizer_fired"] is True
    assert signals["git_commits"]  # other signals still populated
