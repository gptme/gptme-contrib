"""Codex result/workdir regressions, including the public Publisher boundary.

Ported from bob/bughunt-events-cli-extractors and the reviewed #2370 fixes.
No live publisher writes or visibility requests are made.
"""

from __future__ import annotations

import json

import pytest

from gptme_agent_events import CodexExtractor
from gptme_agent_events.publish import Publisher, RepoVisibility


def record(payload, rtype="response_item"):
    return {"type": rtype, "timestamp": "2026-11-12T12:00:00Z", "payload": payload}


def call(cid="c", cmd="git commit -m x", workdir=None, tool="exec"):
    args = {"cmd": cmd}
    if workdir is not None:
        args["workdir"] = workdir
    if tool == "exec":
        return record(
            {
                "type": "custom_tool_call",
                "name": tool,
                "call_id": cid,
                "input": f"text(JSON.stringify(await tools.exec_command({json.dumps(args)})));",
            }
        )
    return record(
        {"type": "function_call", "name": tool, "call_id": cid, "arguments": json.dumps(args)}
    )


def result(cid="c", code=0, text="[main abc1234] x\n", *, blocks=None):
    if blocks is None:
        blocks = [
            "Script completed\nWall time 0.1 seconds\nOutput:\n",
            json.dumps({"exit_code": code, "output": text}, separators=(",", ":")),
        ]
    return record(
        {
            "type": "custom_tool_call_output",
            "call_id": cid,
            "output": [{"type": "input_text", "text": b} for b in blocks],
        }
    )


def kinds(events):
    return [e.kind for e in events]


@pytest.fixture
def ex(monkeypatch):
    # No checkout or network required: two distinct known repos suffice to
    # detect attribution to the session cwd instead of the tool's workdir.
    monkeypatch.setattr(
        "gptme_agent_events.extractors.github_slug",
        lambda directory: {"/session": "o/session", "/worktree": "o/public"}.get(directory),
    )
    extractor = CodexExtractor("rollout", agent="bob")
    extractor.feed(record({"cwd": "/session"}, "turn_context"))
    return extractor


class FakeVisibility(RepoVisibility):
    def lookup(self, slug):
        return slug == "o/public"


@pytest.mark.parametrize("code", [0, 1, 128, "128"])
def test_failed_exec_cannot_publish_a_public_milestone(ex, code):
    rows = []

    def write(row):
        rows.append(row.copy())
        return True

    pub = Publisher(
        {"bob": "bob"}, write, visibility=FakeVisibility(token=""), clock=lambda: 1_000_000.0
    )
    # Start in a public cwd so the original false commit actually passes the
    # visibility gate even before workdir parsing is fixed.
    ex.feed(record({"cwd": "/worktree"}, "turn_context"))
    for ev in ex.feed(call()) + ex.feed(result(code=code)):
        pub.handle(ev)
    assert pub.tick(flush=True) == 1
    row = rows[-1]
    assert row["milestone_kind"] == ("commit" if code == 0 else None)
    assert row["milestone_repo"] == ("o/public" if code == 0 else None)
    assert row["milestone_url"] == (
        "https://github.com/o/public/commit/abc1234" if code == 0 else None
    )
    assert row["private_commits_1h"] == 0


@pytest.mark.parametrize("code", [128, "128", -1])
@pytest.mark.parametrize("shape", ["list", "string", "dict", "metadata"])
def test_failed_structured_results_are_errors(ex, code, shape):
    ex.feed(call(cmd="git commit -m x; git push"))
    data = {"exit_code": code, "output": "fatal: index.lock"}
    if shape == "metadata":
        data = {"metadata": {"exit_code": code}, "output": "fatal: index.lock"}
    out = result(blocks=[json.dumps(data)])
    if shape == "string":
        out["payload"]["output"] = json.dumps(data)
    elif shape == "dict":
        out["payload"]["output"] = data
    assert kinds(ex.feed(out)) == ["error"]


@pytest.mark.parametrize("tool", ["exec", "exec_command"])
@pytest.mark.parametrize("workdir", ["/worktree", "../worktree"])
def test_commit_uses_its_own_workdir_and_unescaped_identity(ex, tool, workdir):
    ex.feed(call(workdir=workdir, tool=tool))
    ex.feed(record({"cwd": "/another-turn"}, "turn_context"))
    (commit,) = ex.feed(result(text="hooks [100%]\n[main abc1234] x\n"))
    assert commit.kind == "commit"
    assert commit.detail["dir"] == "/worktree"
    assert commit.detail["repo"] == "o/public"
    assert commit.detail["sha"] == "abc1234"


def test_two_pending_calls_keep_separate_workdirs(ex):
    ex.feed(call("a", workdir="/worktree"))
    ex.feed(call("b", workdir="/session"))
    (b,) = ex.feed(result("b"))
    (a,) = ex.feed(result("a"))
    assert (a.detail["dir"], a.detail["repo"]) == ("/worktree", "o/public")
    assert (b.detail["dir"], b.detail["repo"]) == ("/session", "o/session")


@pytest.mark.parametrize("workdirs", [(None, "/worktree"), ("/session", "/worktree")])
def test_mixed_workdirs_do_not_attribute_whole_cell_to_one_worktree(ex, workdirs):
    args = []
    for cmd, wd in zip(["git commit -m x", "ls"], workdirs):
        arg = {"cmd": cmd}
        if wd:
            arg["workdir"] = wd
        args.append(f"await tools.exec_command({json.dumps(arg)});")
    ex.feed(
        record(
            {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": "\n".join(args)}
        )
    )
    (commit,) = ex.feed(result())
    assert commit.detail["dir"] == "/session"
    assert commit.detail["repo"] == "o/session"


def test_single_quoted_js_workdir(ex):
    ex.feed(
        record(
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c",
                "input": "await tools.exec_command({cmd: 'git commit -m x', workdir: '/worktree'});",
            }
        )
    )
    (commit,) = ex.feed(result())
    assert commit.detail["repo"] == "o/public"


@pytest.mark.parametrize("shell", ["bash", "/bin/bash", "sh", "zsh"])
def test_legacy_local_shell_unwraps_script(ex, shell):
    ex.feed(
        record(
            {
                "type": "local_shell_call",
                "call_id": "c",
                "action": {"type": "exec", "command": [shell, "-lc", "git push origin HEAD"]},
            }
        )
    )
    assert kinds(ex.feed(result())) == ["push"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("status", [128, "128", "running"])
def test_sibling_success_cannot_mask_failure_or_running(ex, reverse, status):
    ex.feed(call(cmd="git commit -m x; git push"))
    first = {"session_id": 7} if status == "running" else {"exit_code": status}
    blocks = [
        json.dumps({**first, "output": "hooks"}),
        json.dumps({"exit_code": 0, "output": "done"}),
    ]
    if reverse:
        blocks.reverse()
    assert kinds(ex.feed(result(blocks=blocks))) == ([] if status == "running" else ["error"])


def poll(ex, cid, handle, outer=False):
    name, field = ("wait", "cell_id") if outer else ("write_stdin", "session_id")
    ex.feed(
        record(
            {
                "type": "function_call",
                "name": name,
                "call_id": cid,
                "arguments": json.dumps({field: handle}),
            }
        )
    )


@pytest.mark.parametrize("running", ["inner", "cell", "direct", "two_processes"])
def test_running_command_never_lands_and_unknown_poll_stays_unreported(ex, running):
    ex.feed(call(workdir="/worktree"))
    if running == "inner":
        blocks = [json.dumps({"session_id": 7, "output": "hooks"})]
    elif running == "cell":
        # A completed sibling cannot override the cell's running header.
        blocks = ["Script running with cell ID 7\n", json.dumps({"exit_code": 0, "output": "done"})]
    elif running == "two_processes":
        blocks = [json.dumps({"session_id": n, "output": "hooks"}) for n in (7, 8)]
    else:
        blocks = [
            "Chunk ID: abc\nWall time: 1 seconds\nProcess running with session ID 7\nOutput:\n"
        ]
    assert ex.feed(result(blocks=blocks)) == []
    ex.feed(record({"cwd": "/another-turn"}, "turn_context"))
    poll(ex, "poll", 7, outer=running == "cell")
    # Continuation attribution is deliberately outside this repair. Reflogs
    # provide actual landings; a new poll must not invent a successful commit.
    assert ex.feed(result("poll")) == []


@pytest.mark.parametrize("prefix", ["Script running checks", "Process running checks"])
def test_completed_stdout_is_not_a_running_status(ex, prefix):
    ex.feed(call())
    assert kinds(ex.feed(result(text=prefix + "\n[main abc1234] x"))) == ["commit"]


def test_unusable_boolean_exit_code_keeps_fallback_text(ex):
    ex.feed(call())
    assert kinds(
        ex.feed(result(blocks=[json.dumps({"exit_code": True, "stderr": "exited with code 1"})]))
    ) == ["error"]


@pytest.mark.parametrize("code", [0, 128])
def test_follower_reflog_fallback_cannot_publish_failed_commit(tmp_path, code):
    """Exercise poll -> _with_git -> Publisher past the 5s shell fallback.

    A real empty repo has no commit/reflog to corroborate the shell result.
    The success control demonstrates that the fallback path is really active.
    """
    import subprocess

    from gptme_agent_events.follow import Follower, Source
    from gptme_agent_events.gitwatch import GitWatcher

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/public.git"],
        check=True,
    )
    log = tmp_path / "rollout.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(r)
            for r in [record({"cwd": str(repo)}, "turn_context"), call(), result(code=code)]
        )
        + "\n"
    )
    now = [log.stat().st_mtime]

    def clock():
        return now[0]

    git = GitWatcher(agent="bob", clock=clock)
    follower = Follower(
        [Source("codex", tmp_path, ("rollout.jsonl",))],
        agent="bob",
        from_start=True,
        git=git,
        clock=clock,
    )
    rows = []

    def write(row):
        rows.append(row.copy())
        return True

    pub = Publisher({"bob": "bob"}, write, visibility=FakeVisibility(token=""), clock=clock)
    first = follower.poll()
    assert git.watches(str(repo))
    assert "commit" not in kinds(first)  # queued pending reflog matching
    for ev in first:
        pub.handle(ev)
    pub.tick(flush=True)
    now[0] += 6
    second = follower.poll()
    for ev in second:
        pub.handle(ev)
    pub.tick(flush=True)
    assert rows[-1]["milestone_kind"] == ("commit" if code == 0 else None)
    assert rows[-1]["milestone_repo"] == ("o/public" if code == 0 else None)
    assert "error" in kinds(first) if code else "commit" in kinds(second)


@pytest.mark.parametrize("structured", [False, True])
def test_output_parser_preserves_text_and_detects_multiline_running(structured):
    from gptme_agent_events.extractors import _codex_output

    text = (
        "Chunk ID: abc\nWall time: 1 seconds\nProcess running with session ID 7\nOutput:\nhooks\n"
    )
    output = {"session_id": 7, "output": text} if structured else text
    code, running, decoded = _codex_output(output)
    assert code is None
    assert running is True
    assert decoded == text


def test_direct_completed_stdout_running_marker_does_not_override_status(ex):
    ex.feed(call(tool="exec_command"))
    out = result()
    out["payload"]["output"] = (
        "Chunk ID: abc\nWall time: 1 seconds\nProcess exited with code 0\nOutput:\n"
        "Process running with session ID 7\n[main abc1234] x\n"
    )
    assert kinds(ex.feed(out)) == ["commit"]


def test_all_commands_with_same_workdir_can_override_session(ex):
    src = "\n".join(
        f"await tools.exec_command({json.dumps({'cmd': cmd, 'workdir': '/worktree'})});"
        for cmd in ["ls", "git commit -m x"]
    )
    ex.feed(record({"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": src}))
    (commit,) = ex.feed(result())
    assert commit.detail["repo"] == "o/public"


@pytest.mark.parametrize("reverse", [False, True])
def test_known_failure_is_reported_even_with_running_sibling(ex, reverse):
    ex.feed(call())
    blocks = [
        json.dumps({"exit_code": 128, "output": "fatal: index.lock"}),
        json.dumps({"session_id": 7, "output": "hooks"}),
    ]
    if reverse:
        blocks.reverse()
    assert kinds(ex.feed(result(blocks=blocks))) == ["error"]


@pytest.mark.parametrize("sibling_failure", [False, True])
def test_structured_running_stdout_cannot_invent_failure(ex, sibling_failure):
    from gptme_agent_events.extractors import _codex_output

    text = "Process exited with code 1\n"
    blocks = [json.dumps({"session_id": 7, "output": text})]
    if sibling_failure:
        blocks.append(json.dumps({"exit_code": 128, "output": "fatal"}))
    code, running, decoded = _codex_output(blocks)
    assert code == (128 if sibling_failure else None)
    assert running is True
    assert text in decoded
    ex.feed(call())
    assert kinds(ex.feed(result(blocks=blocks))) == (["error"] if sibling_failure else [])


@pytest.mark.parametrize(
    "header, expected_code, running",
    [
        ("Process running with session ID 7\n", None, True),
        ("Wall time: 1 seconds\n", None, False),
        ("Process exited with code 0\n", 0, False),
    ],
)
def test_direct_status_fallback_never_reads_stdout(header, expected_code, running):
    from gptme_agent_events.extractors import _codex_output

    text = "Chunk ID: abc\n" + header + "Output:\nProcess exited with code 1\n"
    assert _codex_output(text) == (expected_code, running, text)


def test_legacy_unframed_exit_status_still_parses():
    from gptme_agent_events.extractors import _codex_output

    text = "Process exited with code 1\n"
    assert _codex_output(text) == (1, False, text)


@pytest.mark.parametrize("real_workdir", [None, "/session"])
def test_workdir_literal_inside_cmd_cannot_override_session(ex, real_workdir):
    ex.feed(call(cmd="echo workdir: '/worktree'; git commit -m x", workdir=real_workdir))
    (commit,) = ex.feed(result())
    assert commit.detail["dir"] == "/session"
    assert commit.detail["repo"] == "o/session"
