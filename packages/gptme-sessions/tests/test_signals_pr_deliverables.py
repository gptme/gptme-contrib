"""PR creation must be attributed to a matching successful tool result."""

import json

import pytest

from gptme_sessions.signals import extract_signals, extract_signals_codex

URL = "https://github.com/example/project/pull/42"


@pytest.fixture
def gptme_pr_creation():
    return [
        {
            "role": "assistant",
            "content": '@shell(create): {"command": "gh pr create --repo example/project --title fix --body fix"}',
        },
        {
            "role": "system",
            "timestamp": "2026-10-17T12:00:00Z",
            "call_id": "create",
            "content": f"Ran command: `gh pr create ...`\n\n```stdout\n{URL}\n```\n",
        },
    ]


@pytest.fixture
def codex_pr_creation():
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "create",
                "arguments": json.dumps({"cmd": "gh pr create --repo example/project"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "create",
                "output": f"Process exited with code 0\nFinal output:\n{URL}\n",
            },
        },
    ]


def _prs(signals):
    return [d for d in signals["deliverable_details"] if d["kind"] == "pull_request"]


def test_gptme_pr_creation(gptme_pr_creation):
    signals = extract_signals(gptme_pr_creation)
    assert signals["prs_submitted"] == ["PR #42"]
    assert URL in signals["deliverables"]
    assert _prs(signals) == [
        {
            "kind": "pull_request",
            "value": URL,
            "provenance_class": "session_committed",
            "evidence": {"source": "trajectory", "tool_name": "shell", "action": "gh_pr_create"},
        }
    ]


def test_codex_pr_creation(codex_pr_creation):
    signals = extract_signals_codex(codex_pr_creation)
    assert signals["prs_submitted"] == ["PR #42"]
    assert URL in signals["deliverables"]
    assert _prs(signals)[0]["evidence"] == {
        "source": "trajectory",
        "tool_name": "exec_command",
        "action": "gh_pr_create",
    }


@pytest.mark.parametrize("tool", ["shell", "gh"])
def test_gptme_native_gh_and_shell(tool, gptme_pr_creation):
    gptme_pr_creation[0]["content"] = f"@{tool}(create): " + json.dumps(
        {
            "command": "pr create --repo example/project"
            if tool == "gh"
            else "gh pr create --repo example/project"
        }
    )
    assert _prs(extract_signals(gptme_pr_creation))[0]["evidence"]["tool_name"] == tool


@pytest.mark.parametrize("code,expected", [(0, 1), (1, 0)])
def test_codex_custom_exec(code, expected, codex_pr_creation):
    codex_pr_creation[0]["payload"] = {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "create",
        "input": 'const r = await tools.exec_command({cmd: "gh pr create --repo example/project"}); text(r.output);',
    }
    codex_pr_creation[1]["payload"].update(
        type="custom_tool_call_output",
        output=[
            {
                "type": "input_text",
                "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"
                + json.dumps({"output": URL, "exit_code": code}),
            }
        ],
    )
    prs = _prs(extract_signals_codex(codex_pr_creation))
    assert len(prs) == expected
    if prs:
        assert prs[0]["value"] == URL


@pytest.mark.parametrize(
    "output",
    [
        URL,
        f"Script completed\nOutput:\n{URL}",
        f"Script completed\nOutput:\nProcess exited with code 1\n{URL}",
        f"Script completed\nOutput:\nReturn code: 1\n{URL}",
        f"Script completed\nOutput:\nexit_code=1\n{URL}",
        f"Script completed\nOutput:\nProcess running with session ID 123\n{URL}",
        f"Script completed\nWall time 0.1 seconds\nOutput:\n{URL}",
    ],
)
def test_codex_custom_exec_requires_confirmed_success(output, codex_pr_creation):
    codex_pr_creation[0]["payload"] = {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "create",
        "input": 'const r = await tools.exec_command({cmd: "gh pr create --repo example/project"}); text(r.output);',
    }
    codex_pr_creation[1]["payload"].update(type="custom_tool_call_output", output=output)
    signals = extract_signals_codex(codex_pr_creation)
    assert _prs(signals) == []
    assert signals["prs_submitted"] == []


@pytest.mark.parametrize("command", ["gh pr view 42", "git push", "cat fixture.txt"])
def test_reading_or_pushing_is_not_creation(command, gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[0]["content"] = "@shell(create): " + json.dumps({"command": command})
    codex_pr_creation[0]["payload"]["arguments"] = json.dumps({"cmd": command})
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_short_circuited_create_cannot_credit_prior_output(gptme_pr_creation, codex_pr_creation):
    command = f"printf '%s\\n' '{URL}'; true || gh pr create --repo example/project"
    gptme_pr_creation[0]["content"] = "@shell(create): " + json.dumps({"command": command})
    codex_pr_creation[0]["payload"]["arguments"] = json.dumps({"cmd": command})
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_unmatched_results_not_credited(gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[1]["call_id"] = "other"
    codex_pr_creation[1]["payload"]["call_id"] = "other"
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_failed_create_not_credited(gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[1]["content"] += "\nReturn code: 1\n"
    codex_pr_creation[1]["payload"]["output"] = f"Process exited with code 1\nFinal output:\n{URL}"
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_new_pr_link_not_a_created_pr(gptme_pr_creation, codex_pr_creation):
    for m in gptme_pr_creation:
        m["content"] = m["content"].replace(
            URL, "https://github.com/example/project/pull/new/branch"
        )
    codex_pr_creation[1]["payload"]["output"] = "https://github.com/example/project/pull/new/branch"
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


@pytest.mark.parametrize("command", ["gh pr create --dry-run", "echo 'gh pr create'"])
def test_not_an_executed_creation(command, gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[0]["content"] = "@shell(create): " + json.dumps({"command": command})
    codex_pr_creation[0]["payload"]["arguments"] = json.dumps({"cmd": command})
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_gptme_native_tool_result_without_timestamp(gptme_pr_creation):
    del gptme_pr_creation[1]["timestamp"]
    assert _prs(extract_signals(gptme_pr_creation))[0]["value"] == URL


def test_codex_async_write_stdin(codex_pr_creation):
    codex_pr_creation[1]["payload"]["output"] = (
        "Process running with session ID 123\nPartial output:\n"
    )
    codex_pr_creation.extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "write_stdin",
                    "call_id": "poll",
                    "arguments": json.dumps({"session_id": 123, "chars": ""}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "poll",
                    "output": f"Process exited with code 0\nFinal output:\n{URL}\n",
                },
            },
        ]
    )
    assert _prs(extract_signals_codex(codex_pr_creation))[0]["value"] == URL


def test_duplicate_results_are_deduplicated(gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation.append(gptme_pr_creation[1].copy())
    codex_pr_creation.append(codex_pr_creation[1].copy())
    assert len(_prs(extract_signals(gptme_pr_creation))) == 1
    assert len(_prs(extract_signals_codex(codex_pr_creation))) == 1


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp/worktree\ngh pr create --repo example/project",
        "cd /tmp/worktree && gh pr create --repo example/project",
        "gh pr create --repo example/project --body 'some details'",
    ],
)
def test_shell_command_boundaries(command, gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[0]["content"] = "@shell(create): " + json.dumps({"command": command})
    codex_pr_creation[0]["payload"]["arguments"] = json.dumps({"cmd": command})
    assert len(_prs(extract_signals(gptme_pr_creation))) == 1
    assert len(_prs(extract_signals_codex(codex_pr_creation))) == 1


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\ngh pr create\n" + URL + "\nEOF",
        "false && gh pr create; gh pr view 42 --json url --jq .url",
    ],
)
def test_unexecuted_create_cannot_authorize_output(command, gptme_pr_creation, codex_pr_creation):
    gptme_pr_creation[0]["content"] = "@shell(create): " + json.dumps({"command": command})
    codex_pr_creation[0]["payload"]["arguments"] = json.dumps({"cmd": command})
    assert _prs(extract_signals(gptme_pr_creation)) == []
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


@pytest.mark.parametrize("tool", ["gh", "shell"])
def test_gptme_existing_pr_error_not_creation(tool, gptme_pr_creation):
    gptme_pr_creation[0]["content"] = f"@{tool}(create): " + json.dumps(
        {"command": "pr create" if tool == "gh" else "gh pr create"}
    )
    gptme_pr_creation[1]["content"] = f"error: a pull request already exists for main\n{URL}\n"
    signals = extract_signals(gptme_pr_creation)
    assert _prs(signals) == []
    assert signals["prs_submitted"] == []


def test_negative_gptme_exit(gptme_pr_creation):
    gptme_pr_creation[1]["content"] += "\nReturn code: -9\n"
    assert _prs(extract_signals(gptme_pr_creation)) == []


def test_running_codex_output_not_committed(codex_pr_creation):
    codex_pr_creation[1]["payload"]["output"] = (
        f"Process running with session ID 123\nPartial output:\n{URL}\n"
    )
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_stdin_new_command_not_creation(codex_pr_creation):
    codex_pr_creation[1]["payload"]["output"] = (
        "Process running with session ID 123\nPartial output:\n"
    )
    codex_pr_creation.extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "write_stdin",
                    "call_id": "poll",
                    "arguments": json.dumps({"session_id": 123, "chars": "gh pr view 42\n"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "poll",
                    "output": f"Process exited with code 0\nFinal output:\n{URL}",
                },
            },
        ]
    )
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


def test_custom_exec_mixed_commands_not_attributed(codex_pr_creation):
    codex_pr_creation[0]["payload"] = {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "create",
        "input": 'await tools.exec_command({cmd: "gh pr create"}); await tools.exec_command({cmd: "gh pr view 42"});',
    }
    codex_pr_creation[1]["payload"].update(type="custom_tool_call_output", output=URL)
    assert _prs(extract_signals_codex(codex_pr_creation)) == []


@pytest.mark.parametrize("code,expected", [(0, 1), (1, 0)])
def test_partial_pr_url_requires_terminal_success(code, expected, codex_pr_creation):
    codex_pr_creation[1]["payload"]["output"] = (
        f"Process running with session ID 123\nPartial output:\n{URL}\n"
    )
    codex_pr_creation.extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "write_stdin",
                    "call_id": "poll",
                    "arguments": json.dumps({"session_id": 123, "chars": ""}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "poll",
                    "output": f"Process exited with code {code}\nFinal output:\n",
                },
            },
        ]
    )
    assert len(_prs(extract_signals_codex(codex_pr_creation))) == expected
