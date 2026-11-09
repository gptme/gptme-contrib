"""Read-only `gh api` calls must not count as GitHub interactions.

Regression: a monitoring session that only listed a PR's comments via
`gh api repos/o/r/issues/N/comments` was graded productive (gh_interactions=1,
comments_posted=1) although it posted nothing.
"""

import pytest

from gptme_sessions.signals import extract_signals_cc, is_productive


def _bash_msgs(*commands: str) -> list[dict]:
    msgs: list[dict] = []
    for i, cmd in enumerate(commands):
        tid = f"bash_{i}"
        msgs.append(
            {
                "type": "assistant",
                "timestamp": f"2026-11-08T06:01:{i:02d}.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}
                    ],
                },
            }
        )
        msgs.append(
            {
                "type": "user",
                "timestamp": f"2026-11-08T06:01:{i:02d}.500Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "is_error": False,
                            "content": "[]",
                        }
                    ],
                },
            }
        )
    return msgs


READS = [
    "gh api repos/gptme/gptme-contrib/issues/2067/comments --jq '.[-4:][] | {u:.user.login}'",
    "gh api repos/o/r/pulls/12/reviews",
    "gh api repos/o/r/pulls/12/comments --paginate | jq length",
    "gh api -X GET repos/o/r/issues/5/comments -f per_page=100",
    "gh api --method=GET repos/o/r/issues/5/comments -F since=2026-01-01",
    # a write elsewhere in the pipeline doesn't make the read a write
    "gh api repos/o/r/issues/5/comments > /tmp/c.json; curl -X POST http://localhost/x",
    # flag-like text inside a quoted value is not a flag
    "gh api repos/o/r/issues/5/comments --jq '.[] | select(.body | contains(\" -f \"))'",
]

WRITES = [
    # quoted "-X GET" inside the body must not mask the real -f write
    "gh api repos/o/r/issues/5/comments -f body='Use -X GET for reads'",
    "gh api -XPOST repos/o/r/issues/5/comments --input r.json",
    "gh api repos/o/r/issues/5/comments -f body='LGTM'",
    "gh api repos/o/r/issues/5/comments --raw-field body=@reply.md",
    "gh api -X POST repos/o/r/pulls/12/reviews --input review.json",
    "gh api repos/o/r/pulls/12/reviews --method POST -f event=APPROVE",
    'gh api repos/o/r/pulls/12/comments --input - <<\'EOF\'\n{"body": "x"}\nEOF',
    "cd /tmp && gh api repos/o/r/issues/5/comments -F body=@c.md | jq .html_url",
    # shell operators inside quotes must not split the endpoint from its write flag
    "gh api repos/o/r/issues/5/comments --jq '.html_url | .' -f body=LGTM",
    'gh api repos/o/r/issues/5/comments --jq ".a; .b" -f body=x',
    "gh api repos/o/r/issues/5/comments --jq '.x && .y' --method POST",
]


@pytest.mark.parametrize("cmd", READS)
def test_gh_api_read_is_not_interaction(cmd: str) -> None:
    sigs = extract_signals_cc(_bash_msgs(cmd))
    assert sigs["gh_interactions"] == 0
    assert sigs.get("comments_posted", 0) == 0
    assert not is_productive(sigs)


@pytest.mark.parametrize("cmd", WRITES)
def test_gh_api_write_is_interaction(cmd: str) -> None:
    sigs = extract_signals_cc(_bash_msgs(cmd))
    assert sigs["gh_interactions"] == 1
    assert is_productive(sigs)


def test_non_api_gh_writes_unchanged() -> None:
    sigs = extract_signals_cc(_bash_msgs("gh pr comment 12 --body 'a; b | c'", "gh issue close 7"))
    assert sigs["gh_interactions"] == 2
    assert sigs["comments_posted"] == 1


def test_split_shell_segments_respects_quotes() -> None:
    from gptme_sessions.signals import _split_shell_segments

    assert _split_shell_segments("a 'x | y' && b \"p; q\" || c; d\ne") == [
        "a 'x | y' ",
        ' b "p; q" ',
        " c",
        " d",
        "e",
    ]
    assert _split_shell_segments(r"a \| b | c") == [r"a \| b ", " c"]
    assert _split_shell_segments("sleep 1 & echo x") == ["sleep 1 & echo x"]
