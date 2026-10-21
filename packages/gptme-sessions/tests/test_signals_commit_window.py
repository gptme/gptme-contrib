"""Commit detection must survive compound commands whose commit line is late.

A gptme shell result is scanned only in its first 500 bytes to avoid reading
files that contain git-log-shaped lines. A compound command (the normal shape
of the email/voice/PM lanes) puts the ``[branch sha] subject`` line thousands of
bytes in, so the commit was never recorded and the session was stored as
outcome=noop. Commit commands are therefore scanned in full.
"""

from gptme_sessions.signals import extract_signals, is_productive

COMMIT_LINE = "[master abc1234] fix: thing"
FILLER = "x" * 800


def _msgs(command: str, output: str, call_id: str | None = "c1"):
    result = {
        "role": "system",
        "timestamp": "2026-10-21T09:00:00Z",
        "content": f"Ran command: `{command}`\n\n```stdout\n{output}\n```\n",
    }
    if call_id is not None:
        result["call_id"] = call_id
    return [
        {"role": "assistant", "content": f'@shell(c1): {{"command": {command!r}}}'},
        result,
    ]


def test_commit_command_scans_full_result():
    """A commit line past the 500-byte window is still recorded."""
    command = "cd /tmp && git-safe-commit --scope-only a.md -m 'fix: thing'"
    signals = extract_signals(_msgs(command, f"{FILLER}\n{COMMIT_LINE}"))

    assert signals["git_commits"] == ["fix: thing (abc1234)"]
    assert is_productive(signals)


def test_commit_command_without_call_id_scans_full_result():
    """The echoed command alone identifies a commit result."""
    command = "git commit -m 'fix: thing'"
    signals = extract_signals(_msgs(command, f"{FILLER}\n{COMMIT_LINE}", call_id=None))

    assert signals["git_commits"] == ["fix: thing (abc1234)"]


def test_non_commit_command_keeps_short_window():
    """A non-commit command still only scans the first 500 bytes."""
    command = "cat notes.md"
    signals = extract_signals(_msgs(command, f"{FILLER}\n{COMMIT_LINE}"))

    assert signals["git_commits"] == []


def test_non_commit_command_keeps_early_commit_line():
    """Existing behaviour inside the window is unchanged."""
    command = "git log --stat"
    signals = extract_signals(_msgs(command, COMMIT_LINE))

    assert signals["git_commits"] == ["fix: thing (abc1234)"]


def test_parse_commit_trailers():
    """--commit-trailers accepts the session_commits.py --trailers-json shape."""
    import click
    import pytest

    from gptme_sessions.cli import _parse_commit_trailers

    assert _parse_commit_trailers(None) is None
    assert _parse_commit_trailers("  ") is None
    assert _parse_commit_trailers('{"abc123": ["sess-1"]}') == {"abc123": ["sess-1"]}

    with pytest.raises(click.BadParameter):
        _parse_commit_trailers("not json")
    with pytest.raises(click.BadParameter):
        _parse_commit_trailers('["abc123"]')
    with pytest.raises(click.BadParameter):
        _parse_commit_trailers('{"abc123": "sess-1"}')


def test_post_session_cli_accepts_commit_trailers(tmp_path, capsys, monkeypatch):
    """The CLI passes commit trailers through to the session record."""
    import sys

    from gptme_sessions.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gptme-sessions",
            "--sessions-dir",
            str(tmp_path),
            "post-session",
            "--harness",
            "gptme",
            "--exit-code",
            "0",
            "--commit-trailers",
            '{"abc123": ["sess-1"]}',
        ],
    )
    assert main() == 0
