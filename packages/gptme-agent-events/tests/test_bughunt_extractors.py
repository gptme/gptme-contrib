"""Regression tests from the 2026-11-24 verified bug hunt (Codex extractor, timestamps).

Record shapes mirror real Codex rollouts: the ``exec`` custom tool returns a list of
``input_text`` blocks, one holding ``JSON.stringify`` of the exec_command result.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from gptme_agent_events import CodexExtractor, GptmeExtractor
from gptme_agent_events.extractors import _github_slug


def codex(rtype, payload, ts="2026-11-12T12:00:00.000Z"):
    return {"type": rtype, "timestamp": ts, "payload": payload}


def exec_call(call_id, cmd, workdir=None):
    args = {"cmd": cmd, "yield_time_ms": 30000, "max_output_tokens": 12000}
    if workdir:
        args["workdir"] = workdir
    src = f"const r = await tools.exec_command({json.dumps(args)});\ntext(JSON.stringify(r));\n"
    return codex(
        "response_item",
        {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": src},
    )


def exec_output(call_id, exit_code, output):
    inner = json.dumps(
        {
            "chunk_id": "33ee69",
            "wall_time_seconds": 0.1,
            "exit_code": exit_code,
            "original_token_count": 10,
            "output": output,
        },
        separators=(",", ":"),
    )
    return codex(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": inner},
            ],
        },
    )


def landings(events):
    return [e.kind for e in events if e.kind in ("commit", "push", "pr_open", "error")]


@pytest.fixture
def two_repos(tmp_path):
    """A private brain checkout (the session cwd) and a public worktree (the workdir)."""
    repos = {}
    for name, origin in (
        ("brain", "git@github.com:ErikBjare/bob.git"),
        ("wt", "git@github.com:gptme/gptme-contrib.git"),
    ):
        d = tmp_path / name
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", origin], check=True)
        repos[name] = d
    _github_slug.cache_clear()
    return repos


# R2-1 -------------------------------------------------------------------------
def test_r2_1_codex_exec_failed_commit_is_error_not_commit():
    """Real rollout: git commit failed with exit 128 (index.lock) -> must not be a commit."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("c1", "git commit -m 'fix: x'"))
    out = ex.feed(
        exec_output("c1", 128, "fatal: Unable to create '.git/index.lock': File exists.\n")
    )
    assert landings(out) == ["error"]


# R2-2 -------------------------------------------------------------------------
def test_r2_2_codex_exec_commit_uses_workdir_not_session_cwd(two_repos):
    """exec_command's ``workdir`` (present on ~94% of real calls) is where git runs."""
    ex = CodexExtractor("r")
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    ex.feed(exec_call("c1", "git commit -m 'fix: x'", workdir=str(two_repos["wt"])))
    (commit,) = ex.feed(exec_output("c1", 0, "[feat/x 6528ea53] fix: x\n 1 file changed\n"))
    assert commit.kind == "commit"
    assert commit.detail.get("dir") == str(two_repos["wt"])
    assert commit.detail.get("repo") == "gptme/gptme-contrib"


# R2-3 -------------------------------------------------------------------------
def test_r2_3_codex_still_running_commit_is_not_reported_as_landed():
    """'Script running with cell ID N' (yield expired, hooks still running) shows no success."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("c1", "git commit -m 'fix: x'"))
    out = ex.feed(
        codex(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": "Script running with cell ID 22\nWall time 10.0 seconds\nOutput:\n",
            },
        )
    )
    assert "commit" not in landings(out)


# R2-4 -------------------------------------------------------------------------
def test_r2_4_codex_exec_commit_sha_is_extracted():
    """Real exec commit output (rollout 2026-10-01): hook output with '[100%]' precedes '[master sha]'."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("c1", "git commit -m 'refactor: x'"))
    (commit,) = ex.feed(
        exec_output(
            "c1",
            0,
            ".....                                                    [100%]\n"
            "9 passed, 63 deselected in 1.44s\n"
            "ruff-format..............................................Passed\n"
            "[master 8eea2ac] refactor: x\n 1 file changed\n",
        )
    )
    assert commit.kind == "commit"
    assert commit.detail.get("sha") == "8eea2ac"


# R2-5 -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ts", ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00"]
)
def test_r2_5_out_of_range_timestamp_does_not_crash_feed(ts):
    """normalize_ts promises 'unparseable values fall back to now'; one odd line must not raise."""
    ex = GptmeExtractor("c")
    events = ex.feed({"role": "assistant", "content": "done", "timestamp": ts})
    assert [e.kind for e in events] == ["turn_end"]


# R2-6 -------------------------------------------------------------------------
def test_r2_6_codex_local_shell_bash_lc_push_is_detected():
    """local_shell_call / legacy shell pass argv ['bash','-lc', script]; the script is the command."""
    ex = CodexExtractor("r")
    ex.feed(
        codex(
            "response_item",
            {
                "type": "local_shell_call",
                "call_id": "c1",
                "action": {"type": "exec", "command": ["bash", "-lc", "git push origin HEAD"]},
            },
        )
    )
    out = ex.feed(
        codex(
            "response_item",
            {"type": "function_call_output", "call_id": "c1", "output": "Exit code: 0\n"},
        )
    )
    assert landings(out) == ["push"]


def test_codex_string_exit_code_is_read_not_reported_as_success():
    """A string ``exit_code`` must not make a failed push look landed.

    Codex sometimes serialises ``exit_code`` as ``"128"``. The structured path
    only accepted ``int``, so the code stayed ``None`` and the caller read the
    result as success.
    """
    from gptme_agent_events.extractors import _codex_output

    block = json.dumps({"exit_code": "128", "output": "error: failed to push some refs"})
    code, running, text = _codex_output([block])
    assert code == 128
    assert running is False
    assert "failed to push" in text


def test_codex_legacy_shell_metadata_exit_code_is_read():
    """The legacy ``shell`` tool nests the status under ``metadata``.

    Reading only the top level left ``code`` None, and the exit regex scanned
    the bare ``output`` string, so a failed push was reported as landed.
    """
    from gptme_agent_events.extractors import _codex_output

    block = json.dumps(
        {
            "output": "error: failed to push some refs",
            "metadata": {"exit_code": 1, "duration_seconds": 0.2},
        }
    )
    code, running, text = _codex_output([block])
    assert code == 1
    assert running is False
    assert "failed to push" in text


def test_codex_unusable_exit_code_keeps_the_block_text():
    """With no ``output`` payload, the raw block stays searchable."""
    from gptme_agent_events.extractors import _codex_output

    block = json.dumps({"exit_code": True, "stderr": "exited with code 1"})
    code, _, text = _codex_output([block])
    assert code == 1
    assert "exited with code 1" in text


# Review findings on gptme/gptme-contrib#2370 ------------------------------------
def test_codex_mixed_workdir_exec_keeps_session_cwd(two_repos):
    """One exec_command without ``workdir`` runs in the session cwd: another call's
    workdir must not re-attribute the whole cell's commit to that worktree."""
    ex = CodexExtractor("r")
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    commit_args = json.dumps({"cmd": "git commit -m 'fix: x'"})
    ls_args = json.dumps({"cmd": "ls", "workdir": str(two_repos["wt"])})
    src = (
        f"const a = await tools.exec_command({commit_args});\n"
        f"const b = await tools.exec_command({ls_args});\n"
        "text(JSON.stringify(a));\n"
    )
    ex.feed(
        codex(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": src},
        )
    )
    (commit,) = ex.feed(exec_output("c1", 0, "[master 6528ea53] fix: x\n 1 file changed\n"))
    assert commit.kind == "commit"
    assert commit.detail.get("dir") != str(two_repos["wt"])
    assert commit.detail.get("repo") != "gptme/gptme-contrib"


def test_codex_inner_exec_command_yield_is_not_reported_as_landed():
    """The JS cell finished but exec_command hit its yield_time_ms: the structured
    result carries ``session_id`` and no ``exit_code``, so the outcome is unknown."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("c1", "git commit -m 'fix: x'"))
    inner = json.dumps(
        {"chunk_id": "a1", "wall_time_seconds": 30.0, "session_id": 7, "output": "running hooks\n"}
    )
    out = ex.feed(
        codex(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 30.0 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": inner},
                ],
            },
        )
    )
    assert landings(out) == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("first_status", [0, 128, "128", "running"])
def test_codex_multiple_results_cannot_hide_failure_or_running(first_status, reverse):
    """A successful sibling result cannot prove the whole exec cell landed."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("c1", "git commit -m 'fix: x'; git push"))
    first: dict[str, str | int] = {
        "output": "running hooks" if first_status == "running" else "fatal: index.lock"
    }
    if first_status == "running":
        first["session_id"] = 7
    else:
        first["exit_code"] = first_status
    blocks = [first, {"exit_code": 0, "output": "Everything up-to-date"}]
    if reverse:
        blocks.reverse()
    out = ex.feed(
        codex(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": json.dumps(b)} for b in blocks],
            },
        )
    )
    expected = ["commit", "push"] if first_status == 0 else ["error"]
    assert landings(out) == ([] if first_status == "running" else expected)


@pytest.mark.parametrize("tool", ["exec", "exec_command"])
def test_codex_relative_workdir_resolves_against_session_cwd(two_repos, tool):
    ex = CodexExtractor("r")
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    if tool == "exec":
        call = exec_call("c1", "git commit -m 'fix: x'", workdir="../wt")
    else:
        call = codex(
            "response_item",
            {
                "type": "function_call",
                "name": tool,
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "git commit -m 'fix: x'", "workdir": "../wt"}),
            },
        )
    ex.feed(call)
    (commit,) = ex.feed(exec_output("c1", 0, "[master 6528ea53] fix: x\n"))
    assert commit.kind == "commit"
    assert commit.detail.get("dir") == str(two_repos["wt"])
    assert commit.detail.get("repo") == "gptme/gptme-contrib"


@pytest.mark.parametrize("wrapped", [False, True, "js_literal"])
@pytest.mark.parametrize("exit_code", [0, 128])
def test_codex_yielded_command_lands_only_after_continuation(two_repos, wrapped, exit_code):
    ex = CodexExtractor("r")
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    ex.feed(exec_call("start", "git commit -m x", workdir=str(two_repos["wt"])))

    running = codex(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "call_id": "start",
            "output": [json.dumps({"session_id": 7, "output": "running hooks"})],
        },
    )
    assert landings(ex.feed(running)) == []
    # The next turn's cwd must not replace the original command's workdir.
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    for call_id in ("poll1", "poll2"):
        args = json.dumps({"session_id": 7, "chars": ""})
        payload = {
            "type": "function_call",
            "name": "write_stdin",
            "call_id": call_id,
            "arguments": args,
        }
        if wrapped:
            if wrapped == "js_literal":
                args = '{session_id: 7, chars: ""}'
            payload = {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": call_id,
                "input": f"text(JSON.stringify(await tools.write_stdin({args})));",
            }
        assert landings(ex.feed(codex("response_item", payload))) == []
        if call_id == "poll1":
            running["payload"]["call_id"] = call_id
            assert landings(ex.feed(running)) == []
    result = exec_output("poll2", exit_code, "[master 6528ea53] x\n")
    events = ex.feed(result)
    assert landings(events) == (["commit"] if exit_code == 0 else ["error"])
    if exit_code == 0:
        assert events[0].detail["sha"] == "6528ea53"
        assert events[0].detail["dir"] == str(two_repos["wt"])
        assert events[0].detail["repo"] == "gptme/gptme-contrib"
    assert ex.feed(result) == []


@pytest.mark.parametrize("outer_cell", [False, True])
def test_codex_text_running_handle_preserves_pr_creation(outer_cell):
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "gh pr create --title x --body x"))
    running = (
        "Script running with cell ID 22" if outer_cell else "Process running with session ID 22"
    )
    assert (
        ex.feed(
            codex(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": running,
                },
            )
        )
        == []
    )
    name = "wait" if outer_cell else "write_stdin"
    args = {"cell_id" if outer_cell else "session_id": "22"}
    ex.feed(
        codex(
            "response_item",
            {
                "type": "function_call",
                "name": name,
                "call_id": "poll",
                "arguments": json.dumps(args),
            },
        )
    )
    (event,) = ex.feed(exec_output("poll", 0, "https://github.com/gptme/gptme/pull/123"))
    assert event.kind == "pr_open"
    assert event.detail["url"] == "https://github.com/gptme/gptme/pull/123"


def test_codex_completed_sibling_failure_survives_continuation():
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x; git push"))
    assert (
        ex.feed(
            codex(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": [
                        json.dumps({"exit_code": 128, "output": "failed commit"}),
                        json.dumps({"session_id": 7, "output": "running push"}),
                    ],
                },
            )
        )
        == []
    )
    ex.feed(
        codex(
            "response_item",
            {
                "type": "function_call",
                "name": "write_stdin",
                "call_id": "poll",
                "arguments": json.dumps({"session_id": 7}),
            },
        )
    )
    assert landings(ex.feed(exec_output("poll", 0, "done"))) == ["error"]


def test_codex_unknown_continuation_cannot_land_another_process():
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x"))
    assert (
        ex.feed(
            codex(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": [json.dumps({"session_id": 7, "output": "running"})],
                },
            )
        )
        == []
    )
    for sid in (8, 7):
        ex.feed(
            codex(
                "response_item",
                {
                    "type": "function_call",
                    "name": "write_stdin",
                    "call_id": str(sid),
                    "arguments": json.dumps({"session_id": sid}),
                },
            )
        )
        out = ex.feed(exec_output(str(sid), 0, "[master 6528ea53] x"))
        assert landings(out) == ([] if sid == 8 else ["commit"])


def test_codex_continuation_keeps_identity_from_early_output():
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x; sleep 60"))
    assert (
        ex.feed(
            codex(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": [json.dumps({"session_id": 7, "output": "[master 6528ea53] x"})],
                },
            )
        )
        == []
    )
    ex.feed(
        codex(
            "response_item",
            {
                "type": "function_call",
                "name": "write_stdin",
                "call_id": "poll",
                "arguments": json.dumps({"session_id": 7}),
            },
        )
    )
    (commit,) = ex.feed(exec_output("poll", 0, ""))
    assert commit.detail["sha"] == "6528ea53"


@pytest.mark.parametrize("prefix", ["Process running checks", "Script running checks"])
def test_codex_completed_stdout_is_not_running(prefix):
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x"))
    assert landings(ex.feed(exec_output("start", 0, prefix + "\n[master 6528ea53] x"))) == [
        "commit"
    ]


def test_codex_single_quoted_workdir(two_repos):
    ex = CodexExtractor("r")
    ex.feed(codex("turn_context", {"cwd": str(two_repos["brain"])}))
    ex.feed(
        codex(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "start",
                "input": f"""text(await tools.exec_command({{cmd: "git commit -m x", workdir: '{two_repos["wt"]}'}}));""",
            },
        )
    )
    (commit,) = ex.feed(exec_output("start", 0, "[master 6528ea53] x"))
    assert commit.detail["dir"] == str(two_repos["wt"])
    assert commit.detail["repo"] == "gptme/gptme-contrib"


def _result_rec(call_id: str, *blocks: str | dict):
    return codex(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [b if isinstance(b, str) else json.dumps(b) for b in blocks],
        },
    )


def _poll(call_id: str, name: str, **args: int | str):
    return codex(
        "response_item",
        {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(args),
        },
    )


@pytest.mark.parametrize("push_exit", [0, 128])
def test_codex_yielded_cell_with_completed_sibling_waits_for_the_cell(push_exit):
    """The outer cell yielded after printing one finished result: the push it is
    still running has not landed."""
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git status; git push"))
    header = "Script running with cell ID 22\nWall time 10.0 seconds\nOutput:\n"
    out = ex.feed(_result_rec("start", header, {"exit_code": 0, "output": "clean"}))
    assert landings(out) == []
    ex.feed(_poll("poll", "wait", cell_id="22"))
    assert landings(ex.feed(exec_output("poll", push_exit, "done"))) == (
        ["push"] if push_exit == 0 else ["error"]
    )


@pytest.mark.parametrize("order", [(7, 8), (8, 7)])
@pytest.mark.parametrize("exits", [(0, 0), (128, 0), (0, 128)])
@pytest.mark.parametrize("parallel_polls", [False, True])
def test_codex_cell_with_two_running_commands_lands_once_both_finish(order, exits, parallel_polls):
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x; git push"))
    out = ex.feed(
        _result_rec(
            "start",
            {"session_id": 7, "output": "[master 6528ea53] x"},
            {"session_id": 8, "output": "running push"},
        )
    )
    assert landings(out) == []
    codes = dict(zip((7, 8), exits))
    if parallel_polls:
        for sid in order:
            ex.feed(_poll(f"p{sid}", "write_stdin", session_id=sid))
    seen = []
    for sid in order:
        if not parallel_polls:
            ex.feed(_poll(f"p{sid}", "write_stdin", session_id=sid))
        events = ex.feed(exec_output(f"p{sid}", codes[sid], "done"))
        seen.append(landings(events))
        if events and events[0].kind == "commit":
            assert events[0].detail["sha"] == "6528ea53"
    if exits == (0, 0):
        assert seen == [[], ["commit", "push"]]
    elif codes[order[0]]:
        # A failure is final: report it at once, and only once.
        assert seen == [["error"], []]
    else:
        assert seen == [[], ["error"]]


def test_codex_yielded_cell_keeps_waiting_for_its_inner_process():
    ex = CodexExtractor("r")
    ex.feed(exec_call("start", "git commit -m x; git push"))
    ex.feed(
        _result_rec(
            "start",
            "Script running with cell ID 22\nOutput:\n",
            {"session_id": 7, "output": "running hooks"},
        )
    )
    ex.feed(_poll("w", "wait", cell_id="22"))
    assert landings(ex.feed(exec_output("w", 0, "pushed"))) == []
    ex.feed(_poll("p", "write_stdin", session_id=7))
    assert landings(ex.feed(exec_output("p", 0, "done"))) == ["commit", "push"]
