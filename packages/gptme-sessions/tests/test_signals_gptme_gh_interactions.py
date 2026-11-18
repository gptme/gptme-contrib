"""gptme trajectories must count GitHub write commands like Claude Code ones do.

``extract_signals`` (gptme format) had no ``gh_interactions`` signal, so a gptme
session whose only output was a PR comment, review or issue close scored
``is_productive() == False`` and was recorded as ``noop``. The Claude Code
extractor has counted these since the signal was introduced; both now use the
same ``_gh_cmd_matches(_GH_INTERACTION_RE, ...)`` rule, including its
read-only ``gh api`` exclusion.
"""

import json

import pytest

from gptme_sessions.signals import extract_signals, extract_signals_cc, is_productive


def _gptme(tool: str, command: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": f"@{tool}(c1): " + json.dumps({"command": command}),
        },
        {
            "role": "system",
            "timestamp": "2026-11-17T13:43:33Z",
            "call_id": "c1",
            "content": "Ran command: `...`\n\n```stdout\nok\n```\n",
        },
    ]


def _cc(command: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        },
    ]


WRITES = [
    "gh pr comment 4970 --repo gptme/gptme --body-file /tmp/note.md",
    "gh pr review 12 --repo o/r --approve --body ok",
    "gh issue close 201 --repo ActivityWatch/aw-core --comment done",
    "gh api -X POST repos/o/r/pulls/1/comments/99/replies -f body=fixed",
    "GH_TOKEN=x timeout 30 gh pr review 5 --repo o/r --approve",
    "cd /tmp/w && gh issue close 3 --repo o/r",
    "/usr/bin/gh pr edit 3 --repo o/r --title t",
    "gh api -X PATCH repos/o/r/issues/comments/123 -F body=@/tmp/status.md",
    # Agent wrapper scripts that post to GitHub without a visible gh call.
    "cat /tmp/b.md | scripts/github/comment-from-stdin.sh gptme/gptme 4967 --no-anti-spam",
    "python3 scripts/github/reply-to-thread.py o/r#5 123 --body-file /tmp/x",
    "uv run --no-sync python3 /abs/scripts/github/reply-to-thread.py o/r#5 1",
    "bash scripts/github/ai-review-now.sh gptme/gptme-cloud 1228",
    "bash /abs/scripts/github/greptile-helper.sh trigger gptme/gptme 4977",
    # Rebase of a PR branch: a force push, no new commit of the agent's own.
    "cd /tmp/w && git push --force-with-lease origin HEAD",
    "timeout 60 git -C /tmp/w push -f origin fix",
    "git push origin +HEAD:fix",
    # Inline shell scripts run their commands (Greptile on #2233).
    "bash -c 'git push --force-with-lease origin HEAD'",
    "timeout 60 bash -lc 'scripts/github/comment-from-stdin.sh o/r 5 < /tmp/b.md'",
    "bash -o pipefail scripts/github/ai-review-now.sh o/r 5",
    # Prefix options whose value is a separate word.
    "env -u VIRTUAL_ENV gh pr comment 5 --repo o/r --body hi",
    "sudo -u bob gh pr comment 5 --repo o/r --body hi",
    "timeout -s KILL 5 gh issue close 3 --repo o/r",
    "sudo -k gh pr comment 5 --repo o/r --body hi",
    # Commands guarded by a compound-command keyword (Greptile on #2233).
    "if true; then gh pr comment 5 --repo o/r --body hi; fi",
    "for n in 1 2; do gh issue close $n --repo o/r; done",
    # xargs runs its command, including an inline shell (Greptile on #2233).
    "printf '5\\n' | xargs -n1 sh -c 'gh pr comment \"$1\" --repo o/r --body hi' _",
    "printf '5\\n' | xargs -I {} gh pr comment {} --repo o/r --body hi",
]
READS = [
    "gh pr view 4970 --repo gptme/gptme --json state",
    "gh api repos/o/r/issues/1/comments",
    "gh pr checks 12 --repo o/r",
    # Quoted mentions only search or print the command text (Greptile on #2233).
    "grep -R 'gh pr comment' .",
    'rg "gh issue close" scripts/',
    "echo 'gh pr merge 5' | wc -l",
    'gh pr view 5 --repo o/r --json body | grep "gh pr comment"',
    "gh api repos/o/r/issues/comments/123",
    "grep -n reply-to-thread.py scripts/",
    "cat scripts/github/comment-from-stdin.sh",
    "bash scripts/github/greptile-helper.sh status gptme/gptme 4977",
    # A plain push is already credited through its commits.
    "git push origin master",
    "rg 'git push --force-with-lease' lessons/",
    # Syntax checks and dry runs change nothing (Greptile on #2233).
    "bash -n scripts/github/comment-from-stdin.sh",
    "bash -o noexec scripts/github/ai-review-now.sh o/r 5",
    "git push --dry-run --force-with-lease origin HEAD",
    "git push -n -f origin fix",
    "bash -c 'git push --dry-run -f origin HEAD'",
    "bash -n -c 'git push --force-with-lease origin HEAD'",
    "bash -o noexec -c 'scripts/github/ai-review-now.sh o/r 5'",
    "bash -nc 'gh pr comment 5 --repo o/r --body hi'",
    # Unquoted gh text that is an argument, not the program run.
    "echo gh pr comment 5",
    "rg gh pr comment .",
    "echo bash -c 'git push --force-with-lease origin HEAD'",
    "echo xargs -n1 sh -c 'gh pr comment 5 --body hi'",
    "printf '5\\n' | xargs -n1 sh -n -c 'gh pr comment \"$1\" --body hi' _",
]


@pytest.mark.parametrize("command", WRITES)
def test_gptme_shell_gh_write_counts(command):
    signals = extract_signals(_gptme("shell", command))
    assert signals["gh_interactions"] == 1
    assert is_productive(signals)


@pytest.mark.parametrize("command", READS)
def test_gptme_shell_gh_read_does_not_count(command):
    signals = extract_signals(_gptme("shell", command))
    assert signals["gh_interactions"] == 0
    assert not is_productive(signals)


def test_gptme_gh_tool_write_counts():
    # The native gh tool receives the subcommand without the leading "gh".
    signals = extract_signals(_gptme("gh", "pr comment 7 --repo o/r --body hi"))
    assert signals["gh_interactions"] == 1


@pytest.mark.parametrize("command", WRITES + READS)
def test_gptme_and_cc_agree(command):
    assert (
        extract_signals(_gptme("shell", command))["gh_interactions"]
        == extract_signals_cc(_cc(command))["gh_interactions"]
    )


def test_rebase_through_push_wrapper_counts():
    """Reduced from gptme PM session 88f01948 (2026-11-17, aw-watcher-web#272).

    It rebased the PR branch and pushed through the review-then-push wrapper
    with an explicit lease, then reported the new head. Before this fix the
    session had 0 commits and 0 gh_interactions and was graded noop.
    """
    command = (
        "cd /tmp/worktrees/aw-watcher-web-feat && /home/bob/bob/.venv/bin/python3 "
        "/home/bob/bob/scripts/github/pm-review-and-push.py --checkout "
        "/tmp/worktrees/aw-watcher-web-feat --review-timeout 300 -- "
        "--force-with-lease=refs/heads/feat/x:b47fea53187558d6478ef90fe3a31ffb4d4e162c "
        "bob HEAD:refs/heads/feat/x"
    )
    signals = extract_signals(_gptme("shell", command))
    assert signals["gh_interactions"] == 1
    assert is_productive(signals)
    assert extract_signals_cc(_cc(command))["gh_interactions"] == 1
