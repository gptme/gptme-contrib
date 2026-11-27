"""Regressions for gptme-contrib#2312 (items 7, 8 and 11)."""

import errno
import os
import re
from pathlib import Path

import pytest
from gptme_runloops.pr_review.github_adapter import _iter_range_lines
from gptme_runloops.team import TeamRun
from gptme_runloops.utils.lock import RunLoopLock


def test_reversed_line_range_still_yields_lines() -> None:
    """No lines meant `raise None`, which aborted publication of the review.

    The declared start stays first: post_inline_finding anchors on it and only
    falls back to the rest of the range.
    """
    assert list(_iter_range_lines("49-47")) == [49, 48, 47]
    assert list(_iter_range_lines("47-49")) == [47, 48, 49]
    assert list(_iter_range_lines("42")) == [42]


def test_failed_pid_write_does_not_leak_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # acquire/release append to a shared history file; keep the test out of it
    monkeypatch.setattr(RunLoopLock, "HISTORY_FILE", tmp_path / "history.log")
    lock = RunLoopLock(tmp_path, "test")

    def no_space(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", no_space)
        with pytest.raises(OSError, match="No space left"):
            lock.acquire()

    assert lock.acquire() is True
    lock.release()


def test_team_prompt_fills_in_the_time(tmp_path: Path) -> None:
    prompt = TeamRun(tmp_path).generate_prompt()
    assert "{timestamp}" not in prompt
    assert re.search(r"\*\*Current Time\*\*: \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", prompt)
