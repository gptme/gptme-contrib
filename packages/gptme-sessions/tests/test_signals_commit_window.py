"""Commit detection must not depend on where the commit line lands in the output.

Regression for gptme sessions that chain work into one shell call, e.g.
``tail -18 README.md && generate.py --write && git-safe-commit --scope-only ...``:
the ``[branch hash] msg`` line lands thousands of bytes into the tool result,
past the 500-byte window the gptme extractor used to scan. The commit was
never recorded, so the session graded as a noop even though it committed
(and pushed) real work.

The widened scan applies only to results whose paired command is a commit
command — the 500-byte guard is kept for every other command, so reading a
file that contains git-log-shaped lines still cannot produce a false positive.
"""

from __future__ import annotations

from gptme_sessions.signals import extract_signals, is_productive

_PAD = "x" * 800
_COMMIT_LINE = "[master 8e8df11cb9] docs(collaboration): scope pilot"


def _msg(role: str, content: str, ts: str = "2026-01-01T10:00:00+00:00", **extra) -> dict:
    return {"role": role, "content": content, "timestamp": ts, **extra}


def _compound_command() -> str:
    return (
        "tail -18 knowledge/technical-designs/README.md && "
        "python3 scripts/generate_technical_designs_appendix.py --write && "
        "git diff --stat -- knowledge/technical-designs/README.md && "
        "/home/bob/bob/bin/git-safe-commit --scope-only "
        "knowledge/technical-designs/README.md -m 'docs: x'"
    )


def test_commit_line_past_500_bytes_is_detected_for_commit_command() -> None:
    """A commit command whose output line sits past 500 bytes still counts."""
    msgs = [
        _msg("user", "commit it", "2026-01-01T10:00:00+00:00"),
        _msg(
            "assistant",
            '@shell(call_1): {"command": "%s"}' % _compound_command(),
            "2026-01-01T10:00:01+00:00",
        ),
        _msg(
            "system",
            f"Ran command: `tail -18 README.md... (1 line)`\n{_PAD}\n{_COMMIT_LINE}\n",
            "2026-01-01T10:00:02+00:00",
            call_id="call_1",
        ),
    ]
    signals = extract_signals(msgs)
    assert len(signals["git_commits"]) == 1
    assert "8e8df11cb9" in signals["git_commits"][0]
    assert is_productive(signals) is True


def test_commit_line_past_500_bytes_is_ignored_for_non_commit_command() -> None:
    """The 500-byte guard still applies to commands that are not commit commands."""
    msgs = [
        _msg("user", "read it", "2026-01-01T10:00:00+00:00"),
        _msg(
            "assistant",
            '@shell(call_2): {"command": "cat some-log.txt"}',
            "2026-01-01T10:00:01+00:00",
        ),
        _msg(
            "system",
            f"Ran command: `cat some-log.txt`\n{_PAD}\n{_COMMIT_LINE}\n",
            "2026-01-01T10:00:02+00:00",
            call_id="call_2",
        ),
    ]
    signals = extract_signals(msgs)
    assert signals["git_commits"] == []


def test_commit_line_within_window_is_detected_as_before() -> None:
    """The narrow-window case keeps working for ordinary commit commands."""
    msgs = [
        _msg("user", "commit it", "2026-01-01T10:00:00+00:00"),
        _msg(
            "assistant",
            '@shell(call_3): {"command": "git commit -m \'docs: x\'"}',
            "2026-01-01T10:00:01+00:00",
        ),
        _msg(
            "system",
            f"Ran command: `git commit -m 'docs: x'`\n{_COMMIT_LINE}\n",
            "2026-01-01T10:00:02+00:00",
            call_id="call_3",
        ),
    ]
    signals = extract_signals(msgs)
    assert len(signals["git_commits"]) == 1
