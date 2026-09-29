"""Commit/write detection must not depend on how the model formatted shell output.

Regression for Claude Code sessions that chain work into one Bash call, e.g.
``cat >> f <<'EOF' ... EOF`` then ``git-safe-commit ... 2>&1 | tail -3``: the
``[branch hash] msg`` line is cut off by ``tail`` so ``_COMMIT_RE`` matches
nothing, and the heredoc write is not a Write-tool call. Both used to grade at
the 0.25 floor even though the session committed and pushed.
"""

from gptme_sessions.signals import extract_signals_cc, grade_signals


def _bash(tool_id: str, command: str, result: str, *, is_error: bool = False) -> list[dict]:
    return [
        {
            "type": "assistant",
            "timestamp": "2026-09-29T15:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-29T15:00:05Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                        "is_error": is_error,
                    }
                ]
            },
        },
    ]


# Shape of session 21e9 (claude-sonnet-5-5): heredoc task append + piped commit/push.
_21E9_CMD = (
    "cat >> tasks/some-task.md <<'EOF'\n- note\nEOF\n"
    "git-safe-commit --scope-only tasks/some-task.md journal/2026-09-29/x.md "
    '-m "docs: x" 2>&1 | tail -3; git-safe-push-master 2>&1 | tail -2'
)
_21E9_RESULT = (
    " 2 files changed, 30 insertions(+)\n"
    "   e294aad05f..52fad08fbe  master -> master\n"
    "✓ Fast-forward pushed 1 commit(s) to origin/master"
)


def test_piped_commit_output_still_counts_commit_and_heredoc_write():
    signals = extract_signals_cc(_bash("t1", _21E9_CMD, _21E9_RESULT))
    assert len(signals["git_commits"]) == 1
    assert "52fad08fbe" in signals["git_commits"][0]
    assert signals["file_writes"] == ["tasks/some-task.md"]
    assert grade_signals(signals) >= 0.6  # was the 0.25 floor


def test_git_safe_push_line_alone_counts_for_commit_command():
    cmd = 'git-safe-commit --scope-only a.md -m "x" 2>&1 | tail -1; git-safe-push-master | tail -1'
    signals = extract_signals_cc(
        _bash("t1", cmd, "✓ Fast-forward pushed 1 commit(s) to origin/master")
    )
    assert len(signals["git_commits"]) == 1


def test_full_commit_line_not_double_counted_with_push():
    result = "[master 52fad08fbe] docs: x\n 1 file changed\n   e294aad05f..52fad08fbe  master -> master\n"
    signals = extract_signals_cc(_bash("t1", 'git commit -m "x" && git push', result))
    assert signals["git_commits"] == ["docs: x (52fad08fbe)"]


def test_push_hash_deduped_against_earlier_direct_commit():
    msgs = _bash("t1", 'git commit -m "x"', "[master 52fad08fbe] docs: x\n")
    msgs += _bash(
        "t2",
        "git commit --allow-empty -m y | tail -1; git push",
        "   e294aad05f..52fad08fbe  master -> master\n",
    )
    signals = extract_signals_cc(msgs)
    assert len(signals["git_commits"]) == 1


def test_push_or_fetch_output_without_commit_command_is_not_a_commit():
    signals = extract_signals_cc(
        _bash("t1", "git push 2>&1 | tail -1", "   e294aad05f..52fad08fbe  master -> master\n")
    )
    assert signals["git_commits"] == []
    signals = extract_signals_cc(
        _bash(
            "t1",
            "git commit -m x | tail -1; git fetch",
            "   e294aad05f..52fad08fbe  master     -> origin/master\n",
        )
    )
    assert signals["git_commits"] == []


def test_failed_or_unconfirmed_commit_is_not_credited():
    for result in (
        "nothing to commit, working tree clean",
        "⚠️  git-safe-commit: COMMIT OUTCOME UNCONFIRMED (exit 1)\n   a1b2c3d4e5..f6a7b8c9d0  master -> master",
    ):
        signals = extract_signals_cc(
            _bash("t1", "git-safe-commit --scope-only a -m x | tail -2", result)
        )
        assert signals["git_commits"] == [], result


def test_diffstat_from_git_diff_is_not_commit_evidence():
    cmd = "git diff --stat a.md; git-safe-commit --scope-only a.md -m x 2>&1 | tail -1"
    signals = extract_signals_cc(
        _bash("t1", cmd, " 1 file changed, 4 insertions(+)\nscope-checked 1 file(s)")
    )
    assert signals["git_commits"] == []
    # ...but the same diffstat from the commit itself counts.
    signals = extract_signals_cc(
        _bash(
            "t1",
            "git-safe-commit --scope-only a.md -m x 2>&1 | tail -2",
            " 1 file changed, 4 insertions(+)\n create mode 100644 a.md",
        )
    )
    assert len(signals["git_commits"]) == 1


def test_heredoc_write_variants_and_exclusions():
    cmd = (
        "cat > src/a.py <<'EOF'\nx\nEOF\n"
        "cat <<EOF >> docs/b.md\ny\nEOF\n"
        "tee -a notes/c.txt <<'EOF'\nz\nEOF\n"
        "cat > /tmp/scratch.py <<'EOF'\nq\nEOF\n"
        "cat > /tmp/worktrees/repo/d.py <<'EOF'\nw\nEOF\n"
        "some_cmd > out.log\n"
    )
    signals = extract_signals_cc(_bash("t1", cmd, ""))
    assert signals["file_writes"] == [
        "src/a.py",
        "/tmp/worktrees/repo/d.py",
        "docs/b.md",
        "notes/c.txt",
    ]


def test_heredoc_journal_write_is_not_a_file_write():
    cmd = "cat > journal/2026-09-29/session.md <<'EOF'\nx\nEOF"
    signals = extract_signals_cc(_bash("t1", cmd, ""))
    assert signals["file_writes"] == []
