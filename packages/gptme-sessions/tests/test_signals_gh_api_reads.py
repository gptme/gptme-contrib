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
    # reads inside process substitutions stay reads
    "diff <(gh api repos/o/r/issues/5/comments) <(gh api repos/o/r/issues/6/comments)",
    "gh api repos/gptme/gptme-contrib/issues/2067/comments --jq '.[-4:][] | {u:.user.login}'",
    "gh api repos/o/r/pulls/12/reviews",
    "gh api repos/o/r/pulls/12/comments --paginate | jq length",
    "gh api -X GET repos/o/r/issues/5/comments -f per_page=100",
    "gh api --method=GET repos/o/r/issues/5/comments -F since=2026-01-01",
    # a write elsewhere in the pipeline doesn't make the read a write
    "gh api repos/o/r/issues/5/comments > /tmp/c.json; curl -X POST http://localhost/x",
    # a backgrounded read is its own command; curl's -X POST is not its method
    "gh api repos/o/r/issues/5/comments & curl -X POST http://localhost/x",
    "gh api repos/o/r/issues/5/comments 2>&1 | head",
    # flag-like text inside a quoted value is not a flag
    "gh api repos/o/r/issues/5/comments --jq '.[] | select(.body | contains(\" -f \"))'",
    # wrapper commands and absolute paths still resolve to gh api
    "env gh api --paginate repos/o/r/issues/5/comments",
    "env -i GH_TOKEN=x /usr/bin/gh api repos/o/r/issues/5/comments",
    "command gh api repos/o/r/pulls/12/reviews",
    # shell keywords, grouping and arg-taking wrappers before gh api
    "if gh api repos/o/r/issues/5/comments; then echo found; fi",
    "! gh api repos/o/r/issues/5/comments >/dev/null",
    "(gh api repos/o/r/issues/5/comments)",
    # grouping punctuation is not part of the method value (``GET)`` is GET)
    "(gh api repos/o/r/issues/5/comments -f per_page=100 -X GET)",
    "{ gh api repos/o/r/issues/5/comments -f per_page=100 -X GET; }",
    "x=$(gh api repos/o/r/issues/5/comments -f per_page=100 --method GET)",
    "timeout 30 gh api repos/o/r/pulls/12/reviews",
    # option values that look like write flags are values, not flags
    "gh api repos/o/r/issues/5/comments --template '-f'",
    "gh api repos/o/r/issues/5/comments -q '-F' -H 'Accept: -f'",
    # a write flag in a trailing shell comment is never executed
    "gh api --paginate repos/o/r/issues/5/comments # -f body=example",
    # quoted example text is not an invocation
    "printf '%s\\n' 'gh api repos/o/r/issues/5/comments'",
    'echo "gh api repos/o/r/issues/5/comments -f body=x"',
    # operators inside a comment do not start a new command
    "gh api repos/o/r/issues/5/comments # example only; gh api repos/o/r/issues/5/comments -f body=x",
    "gh api repos/o/r/issues/5/comments # a | gh api repos/o/r/issues/5/comments -f body=x",
    "x=$(gh api repos/o/r/issues/5/comments --jq length)",
    # a quoted substitution executes, but this one is a GET
    'result="$(gh api repos/o/r/issues/5/comments --jq length)"',
    # single-quoted, escaped or commented substitutions never execute
    "printf '%s' '$(gh api repos/o/r/issues/5/comments -f body=x)'",
    'echo "\\$(gh api repos/o/r/issues/5/comments -f body=x)"',
    "true # $(gh api repos/o/r/issues/5/comments -f body=x)",
    # HEAD fetches headers only and posts nothing
    "gh api -X HEAD repos/o/r/issues/5/comments",
    "gh api --method=head repos/o/r/issues/5/comments -f per_page=1",
    # an inline -c script that only reads is still a read
    "bash -c 'gh api repos/o/r/issues/5/comments --jq length'",
    # without -c the operand is a script file, and quoted text after it is data
    "bash run.sh 'gh api repos/o/r/issues/5/comments -f body=x'",
    # gh rejects an unknown shorthand before sending anything
    "gh api repos/o/r/issues/5/comments -sf body=LGTM",
    # a quoted ')' is an option value, not the end of an enclosing group
    "(gh api repos/o/r/issues/5/comments --template ')' -X GET)",
    # ``--`` ends options: a later -f is the endpoint's positional, not a field
    "gh api -- repos/o/r/issues/5/comments",
    # heredoc bodies are data: a documented gh api write is not a write
    "cat > doc.md <<'EOF'\ngh api repos/o/r/issues/5/comments -f body=x\nEOF",
    "cat <<'EOF'\n$(gh api -X POST repos/o/r/issues/5/comments -f body=x)\nEOF",
    'cat <<"EOF"\n`gh api repos/o/r/issues/5/comments -f body=x`\nEOF',
    "cat <<EOF\ngh api repos/o/r/issues/5/comments -f body=x\nEOF",
    "cat <<-'EOF'\n\tgh api repos/o/r/issues/5/comments -f body=x\n\tEOF",
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
    "env gh api repos/o/r/issues/5/comments -f body=LGTM",
    "if gh api repos/o/r/issues/5/comments -f body=x; then echo ok; fi",
    "timeout 30 gh api repos/o/r/issues/5/comments --jq .id -f body=x",
    # a comment ends at the newline; the next line runs
    "true # note; x\ngh api repos/o/r/issues/5/comments -f body=x",
    # an escaped or quoted # is not a comment
    "gh api repos/o/r/issues/5/comments -f body=issue\\#5 -f x=y",
    "gh api repos/o/r/issues/5/comments -f body='see #5; ok'",
    "x=$(gh api repos/o/r/issues/5/comments -f body=x)",
    "y=`gh api repos/o/r/issues/5/comments -f body=x`",
    # bash executes substitutions inside double quotes too
    'result="$(gh api repos/o/r/issues/5/comments -f body=x)"',
    'y="`gh api repos/o/r/issues/5/comments -f body=x`"',
    "echo \"posted: $(gh api repos/o/r/issues/5/comments -f body='a) b' --jq .id)\"",
    'x="$(echo "$(gh api repos/o/r/issues/5/comments -f body=x)")"',
    "(gh api repos/o/r/issues/5/comments -f body=x -X POST)",
    "(gh api repos/o/r/issues/5/comments -f body=x)",
    # a mid-word # is literal in bash, not a comment that hides -f
    "gh api repos/o/r/issues/5/comments -H X-Issue:#5 -f body=x",
    "gh api repos/o/r/issues/5/comments -H 'X-Issue:#5' -f body=x",
    # the inner GET of a process substitution is a separate command; the outer
    # invocation still POSTs its --input
    "gh api repos/o/r/issues/5/comments --input <(gh api -X GET repos/o/r/issues/comments/123)",
    "gh api repos/o/r/issues/5/comments --input reply.json",
    # a write inside a process substitution counts as its own command
    "cat <(gh api repos/o/r/issues/5/comments -f body=x)",
    # bash runs an inline -c script; the quoted script is still a command
    "bash -c 'gh api repos/o/r/issues/5/comments -f body=LGTM'",
    "timeout 30 bash -lc 'cd /tmp && gh api repos/o/r/issues/5/comments -f body=x'",
    'sh -o errexit -c "gh api repos/o/r/issues/5/comments -f body=x"',
    "/bin/bash -e -c 'x=$(gh api repos/o/r/issues/5/comments -f body=x)'",
    # a quoted ')' option value must not truncate the field that follows it
    "gh api repos/o/r/issues/5/comments --template ')' -f body=x",
    "gh api repos/o/r/issues/5/comments -q ')' -f body=x",
    # short flags cluster the way gh's flag parser (pflag) allows
    "gh api repos/o/r/issues/5/comments -if body=LGTM",
    "gh api repos/o/r/issues/5/comments -ifbody=LGTM",
    "gh api -iX POST repos/o/r/pulls/12/reviews",
    "gh api -X=POST repos/o/r/pulls/12/reviews",
    # the command after a heredoc terminator still runs, apostrophes in the body
    # notwithstanding
    "cat <<'EOF'\nDon't post yet\nEOF\ngh api repos/o/r/issues/5/comments -f body=x",
    # an unquoted delimiter still expands substitutions in the body
    "cat <<EOF\nid: $(gh api repos/o/r/issues/5/comments -f body=x --jq .id)\nEOF",
    # quotes and comment markers are literal data in an unquoted heredoc
    "cat <<EOF\n'$(gh api repos/o/r/issues/5/comments -f body=x)'\nEOF",
    "cat <<EOF\n# $(gh api repos/o/r/issues/5/comments -f body=x)\nEOF",
    # heredoc redirect operands are shell syntax, not gh shorthand flags
    'gh api repos/o/r/issues/5/comments --input - <<-\'EOF\'\n\t{"body":"x"}\n\tEOF',
    # a here-string is not a heredoc
    'gh api repos/o/r/issues/5/comments --input - <<< \'{"body": "x"}\'',
    "cat <<< 'EOF'\ngh api repos/o/r/issues/5/comments -f body=x",
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


def test_non_api_write_fed_by_gh_api_read_counts() -> None:
    # the comment is a write even though its body comes from a gh api read
    cmd = "gh pr comment 12 --body-file <( gh api repos/o/r/issues/5/comments --jq '.[0].body' )"
    sigs = extract_signals_cc(_bash_msgs(cmd))
    assert sigs["gh_interactions"] == 1
    assert sigs["comments_posted"] == 1


def test_gh_api_text_in_comment_body_still_counts() -> None:
    # "gh api" mentioned inside a gh pr comment body is not a gh api invocation
    sigs = extract_signals_cc(_bash_msgs("gh pr comment 12 --body 'Use gh api to inspect this PR'"))
    assert sigs["gh_interactions"] == 1
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
    # lone & backgrounds and terminates a command; redirects keep their &
    assert _split_shell_segments("sleep 1 & echo x") == ["sleep 1 ", " echo x"]
    assert _split_shell_segments("a 2>&1 b &> f >&2") == ["a 2>&1 b &> f >&2"]
    # unquoted comments swallow operators up to the newline; a#b is a word
    assert _split_shell_segments("a # x; y | z\nb") == ["a ", "b"]
    assert _split_shell_segments("a#b; c '#d; e'") == ["a#b", " c '#d; e'"]


def test_quoted_substitution_write_counts_comment() -> None:
    # Bash runs "$(...)" inside double quotes; the quoted and unquoted forms
    # must grade the same (1 interaction, 1 comment).
    for cmd in (
        'result="$(gh api repos/o/r/issues/5/comments -f body=x)"',
        "result=$(gh api repos/o/r/issues/5/comments -f body=x)",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs["gh_interactions"] == 1, cmd
        assert sigs["comments_posted"] == 1, cmd


def test_endpoint_in_field_value_is_not_comment() -> None:
    # PATCHing an issue whose body mentions a comments path posts no comment;
    # the endpoint argument decides, not text inside a field value.
    for body in ("see repos/o/r/issues/6/comments", "plain text"):
        cmd = f"gh api -X PATCH repos/o/r/issues/5 -f body='{body}'"
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs.get("comments_posted", 0) == 0, cmd
        assert sigs["gh_interactions"] == 0, cmd


def test_bash_c_write_counts_comment() -> None:
    # The same write graded directly and through an inline -c script.
    for cmd in (
        "gh api repos/o/r/issues/5/comments -f body=LGTM",
        "bash -c 'gh api repos/o/r/issues/5/comments -f body=LGTM'",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs["gh_interactions"] == 1, cmd
        assert sigs["comments_posted"] == 1, cmd


def test_unquoted_substitution_arg_does_not_lend_method() -> None:
    # An inner GET in an unquoted $(...) or backtick argument is its own
    # command; the outer POST still writes (same as the quoted/literal forms).
    for cmd in (
        "gh api repos/o/r/issues/5/comments -f body=$(gh api -X GET user --jq .login)",
        "gh api repos/o/r/issues/5/comments -f body=`gh api -X GET user --jq .login`",
        'gh api repos/o/r/issues/5/comments -f body="$(gh api -X GET user --jq .login)"',
        "gh api repos/o/r/issues/5/comments -f body=alice",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs["gh_interactions"] == 1, cmd
        assert sigs["comments_posted"] == 1, cmd


def test_unquoted_substitution_read_only_stays_zero() -> None:
    # Read control: a GET whose argument comes from another GET posts nothing.
    for cmd in (
        "gh api -X GET repos/o/r/issues/$(gh api -X GET user --jq .id)/comments",
        "gh api repos/o/r/issues/5/comments --jq $(echo .)",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs.get("comments_posted", 0) == 0, cmd
        assert sigs["gh_interactions"] == 0, cmd


def test_pipeline_inside_substitution_keeps_outer_method() -> None:
    # The pipe inside $(...) belongs to the inner command; splitting on it
    # would drop the outer ``-X GET`` and count a read as a write.
    for cmd in (
        "gh api repos/o/r/issues/5/comments -f per_page=$(printf 100 | cat) -X GET",
        "gh api repos/o/r/issues/5/comments -f per_page=`printf 100 | cat` -X GET",
        'gh api repos/o/r/issues/5/comments -f per_page="$(printf 100 | cat)" -X GET',
        "gh api repos/o/r/issues/5/comments -f per_page=100 -X GET",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs.get("comments_posted", 0) == 0, cmd
        assert sigs["gh_interactions"] == 0, cmd


def test_single_quoted_backtick_is_literal() -> None:
    # A backtick inside single quotes is jq data, not a substitution, so the
    # trailing ``-f body=x`` still makes this a write.
    for cmd in (
        "gh api repos/o/r/issues/5/comments --jq '\"`\"' -f body=x",
        "gh api repos/o/r/issues/5/comments --jq '\"x\"' -f body=x",
    ):
        sigs = extract_signals_cc(_bash_msgs(cmd))
        assert sigs["gh_interactions"] == 1, cmd
        assert sigs["comments_posted"] == 1, cmd
