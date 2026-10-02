"""Commit/write detection must not depend on how the model formatted shell output.

Regression for Claude Code sessions that chain work into one Bash call, e.g.
``cat >> f <<'EOF' ... EOF`` then ``git-safe-commit ... 2>&1 | tail -3``: the
``[branch hash] msg`` line is cut off by ``tail`` so ``_COMMIT_RE`` matches
nothing, and the heredoc write is not a Write-tool call. Both used to grade at
the 0.25 floor even though the session committed and pushed.
"""

from gptme_sessions.signals import extract_signals_cc, grade_signals


def _bash(
    tool_id: str,
    command: str,
    result: str,
    *,
    is_error: bool = False,
    cwd: str | None = None,
) -> list[dict]:
    return [
        {
            "type": "assistant",
            "cwd": cwd,
            "timestamp": "2026-09-29T15:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": command, "cwd": cwd},
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
    signals = extract_signals_cc(_bash("t1", _21E9_CMD, _21E9_RESULT, cwd="/home/bob/bob"))
    assert len(signals["git_commits"]) == 1
    assert "52fad08fbe" in signals["git_commits"][0]
    assert signals["file_writes"] == ["tasks/some-task.md"]
    detail = next(detail for detail in signals["deliverable_details"] if detail["kind"] == "commit")
    assert detail["evidence"]["subject"] == "docs: x"
    assert detail["evidence"]["repo"] == "/home/bob/bob"
    assert grade_signals(signals) >= 0.6  # was the 0.25 floor


def test_quiet_mutations_preserve_reconciliation_evidence():
    commit_cmd = (
        'git -C /tmp/worktrees/r commit -m "fix: quiet" 2>&1 | tail -1; '
        "git -C /tmp/worktrees/r push -q fork feature >/dev/null"
    )
    msgs = _bash("commit123456", commit_cmd, "", cwd="/home/bob/bob")
    msgs += _bash(
        "pr123456",
        'gh pr create --repo Org/r --head me:feature --title "fix: quiet" --body body >/dev/null',
        "",
        cwd="/tmp/worktrees/r",
    )

    signals = extract_signals_cc(msgs)

    commit = next(detail for detail in signals["deliverable_details"] if detail["kind"] == "commit")
    assert commit["evidence"]["subject"] == "fix: quiet"
    assert commit["evidence"]["repo"] == "/tmp/worktrees/r"
    assert commit["evidence"]["quiet_push"] is True
    # The push is absorbed into the commit evidence (quiet_push=True); no separate push detail.
    assert not any(detail["kind"] == "push" for detail in signals["deliverable_details"])
    pull_request = next(
        detail for detail in signals["deliverable_details"] if detail["kind"] == "pull_request"
    )
    assert pull_request["evidence"]["repo"] == "Org/r"
    assert pull_request["evidence"]["head"] == "me:feature"
    assert pull_request["evidence"]["title"] == "fix: quiet"
    assert pull_request["evidence"]["cwd"] == "/tmp/worktrees/r"
    assert signals["prs_submitted"] == ["PR (output swallowed)"]


def test_pr_create_failure_text_is_not_credited_as_swallowed_output():
    signals = extract_signals_cc(
        _bash(
            "pr123456",
            "gh pr create --draft --title WIP",
            "PR creation failed: already exists",
        )
    )
    assert signals["prs_submitted"] == []
    assert not any(detail["kind"] == "pull_request" for detail in signals["deliverable_details"])


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
    # Ordered by first appearance in the command (deterministic across the
    # tree-sitter and regex extractors).
    assert signals["file_writes"] == [
        "src/a.py",
        "docs/b.md",
        "notes/c.txt",
        "/tmp/worktrees/repo/d.py",
    ]


def test_heredoc_journal_write_is_not_a_file_write():
    cmd = "cat > journal/2026-09-29/session.md <<'EOF'\nx\nEOF"
    signals = extract_signals_cc(_bash("t1", cmd, ""))
    assert signals["file_writes"] == []


def test_commit_heredoc_subject_extracted():
    """Heredoc commit message subject should be captured even when output is truncated."""
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: quiet heredoc\n\n"
        "body line\n"
        "EOF\n"
        ')" 2>&1 | tail -1'
    )
    signals = extract_signals_cc(_bash("t1", cmd, " 1 file changed, 1 insertion(+)"))
    assert len(signals["git_commits"]) == 1
    detail = next(d for d in signals["deliverable_details"] if d["kind"] == "commit")
    assert detail["evidence"]["subject"] == "fix: quiet heredoc"


def test_standalone_quiet_push_is_credited():
    """A standalone git push -q (no commit in same command) should be a push deliverable."""
    cmd = "git push -q origin master >/dev/null 2>&1"
    signals = extract_signals_cc(_bash("t1", cmd, ""))
    assert any(d["kind"] == "push" for d in signals["deliverable_details"])
