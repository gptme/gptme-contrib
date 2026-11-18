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
]
READS = [
    "gh pr view 4970 --repo gptme/gptme --json state",
    "gh api repos/o/r/issues/1/comments",
    "gh pr checks 12 --repo o/r",
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
