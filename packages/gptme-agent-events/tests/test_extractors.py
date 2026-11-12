"""Extractors turn harness records into the shared event schema."""

from __future__ import annotations

import json
import subprocess

import pytest

from gptme_agent_events import ClaudeCodeExtractor, CodexExtractor, GptmeExtractor
from gptme_agent_events.event import normalize_ts
from gptme_agent_events.extractors import github_slug, gptme_result_failed, landing_detail


def kinds(events):
    return [(e.kind, e.tool_class) if e.kind == "tool" else e.kind for e in events]


def cc_assistant(blocks, stop_reason="tool_use", mid="msg_1", **extra):
    return {
        "type": "assistant",
        "sessionId": "sess-cc",
        "timestamp": "2026-11-12T10:00:00.000Z",
        "message": {"id": mid, "stop_reason": stop_reason, "content": blocks},
        **extra,
    }


def cc_result(tool_use_id, is_error=False):
    return {
        "type": "user",
        "sessionId": "sess-cc",
        "timestamp": "2026-11-12T10:00:01.000Z",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error}]
        },
    }


def bash(call_id, command):
    return {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}


class TestClaudeCode:
    def test_tool_classes_and_session_id(self):
        ex = ClaudeCodeExtractor("file-stem", agent="bob")
        events = ex.feed(
            cc_assistant(
                [
                    {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
                    {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "/y"}},
                    bash("c", "uv run pytest tests/"),
                    {"type": "tool_use", "id": "d", "name": "mcp__x__y", "input": {}},
                ]
            )
        )
        assert kinds(events) == [
            ("tool", "read"),
            ("tool", "write"),
            ("tool", "test"),
            ("tool", "other"),
        ]
        assert {e.session for e in events} == {"sess-cc"}
        assert {e.agent for e in events} == {"bob"}
        # Only writes carry a path; commands are never copied into events.
        assert events[0].detail == {}
        assert events[1].detail == {"path": "/y"}
        assert "pytest" not in json.dumps([e.to_dict() for e in events])

    def test_commit_and_push_reported_on_success(self):
        ex = ClaudeCodeExtractor("s")
        ex.feed(cc_assistant([bash("c1", "git commit -m 'x' && git push origin HEAD")]))
        assert kinds(ex.feed(cc_result("c1"))) == ["commit", "push"]

    def test_failed_call_is_error_not_commit(self):
        ex = ClaudeCodeExtractor("s")
        ex.feed(cc_assistant([bash("c1", "git commit -m x")]))
        events = ex.feed(cc_result("c1", is_error=True))
        assert kinds(events) == ["error"]
        assert events[0].tool == "Bash"

    def test_unknown_result_ignored(self):
        assert ClaudeCodeExtractor("s").feed(cc_result("nope")) == []

    def test_turn_end_once_per_message(self):
        ex = ClaudeCodeExtractor("s")
        # One message written as two rows (thinking + text) with the same id.
        a = ex.feed(cc_assistant([{"type": "thinking"}], stop_reason="end_turn", mid="m9"))
        b = ex.feed(cc_assistant([{"type": "text"}], stop_reason="end_turn", mid="m9"))
        assert kinds(a + b) == ["turn_end"]

    def test_sidechain_marked_subagent(self):
        ex = ClaudeCodeExtractor("s")
        events = ex.feed(cc_assistant([bash("x", "ls")], isSidechain=True))
        assert events[0].subagent is True

    def test_pr_create(self):
        ex = ClaudeCodeExtractor("s")
        ex.feed(cc_assistant([bash("p", "gh pr create --title t --body b")]))
        assert kinds(ex.feed(cc_result("p"))) == ["pr_open"]
        ex.feed(cc_assistant([bash("q", "gh pr create --dry-run")]))
        assert ex.feed(cc_result("q")) == []

    def test_ignores_other_record_types(self):
        ex = ClaudeCodeExtractor("s")
        assert ex.feed({"type": "permission-mode"}) == []
        assert ex.feed({"type": "assistant", "message": "garbage"}) == []


def gptme_msg(role, content, call_id=None):
    d = {"role": role, "content": content, "timestamp": "2026-11-12T11:00:00"}
    if call_id:
        d["call_id"] = call_id
    return d


class TestGptme:
    def test_tool_format_calls_and_results(self):
        ex = GptmeExtractor("2026-11-12-some-convo")
        events = ex.feed(
            gptme_msg(
                "assistant",
                "Let me commit.\n"
                '@shell(call_1): {"command": "git commit -m done && git push"}\n'
                '@save(call_2): {"path": "notes.md", "content": "hi"}',
            )
        )
        assert kinds(events) == [("tool", "shell"), ("tool", "write")]
        assert events[1].detail == {"path": "notes.md"}
        assert kinds(ex.feed(gptme_msg("system", "Ran command\n", "call_1"))) == ["commit", "push"]
        assert ex.feed(gptme_msg("system", "Saved", "call_2")) == []

    def test_failed_shell_is_error(self):
        ex = GptmeExtractor("c")
        ex.feed(gptme_msg("assistant", '\n@shell(call_1): {"command": "git commit -m x"}'))
        out = ex.feed(gptme_msg("system", "Ran command\n\nReturn code: 1\n", "call_1"))
        assert kinds(out) == ["error"]

    def test_gh_tool_pr_create(self):
        ex = GptmeExtractor("c")
        ex.feed(gptme_msg("assistant", '\n@gh(call_9): {"command": "pr create --fill"}'))
        assert kinds(ex.feed(gptme_msg("system", "https://github.com/o/r/pull/1", "call_9"))) == [
            "pr_open"
        ]

    def test_markdown_format(self):
        ex = GptmeExtractor("c")
        events = ex.feed(
            gptme_msg("assistant", "Running:\n```shell\ngit commit -m x\n```\n```save a.py\nx\n```")
        )
        # No call id to pair with, so the commit is reported with the call.
        assert kinds(events) == [("tool", "shell"), "commit", ("tool", "write")]

    def test_reply_without_tools_ends_turn(self):
        assert kinds(GptmeExtractor("c").feed(gptme_msg("assistant", "All done."))) == ["turn_end"]

    def test_session_from_dir_name(self):
        ev = GptmeExtractor("2026-11-12-crawling-red-mermaid").feed(gptme_msg("assistant", "ok"))
        assert ev[0].session == "2026-11-12-crawling-red-mermaid"


def codex(rtype, payload, ts="2026-11-12T12:00:00.000Z"):
    return {"type": rtype, "timestamp": ts, "payload": payload}


class TestCodex:
    def test_session_meta_and_exec_command(self):
        ex = CodexExtractor("rollout-x")
        ex.feed(codex("session_meta", {"id": "codex-sess"}))
        events = ex.feed(
            codex(
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": json.dumps({"cmd": "git commit -m x"}),
                },
            )
        )
        assert kinds(events) == [("tool", "shell")]
        assert events[0].session == "codex-sess"
        out = ex.feed(
            codex(
                "response_item",
                {"type": "function_call_output", "call_id": "c1", "output": "Exit code: 0\n"},
            )
        )
        assert kinds(out) == ["commit"]

    def test_exec_js_source_and_failure(self):
        ex = CodexExtractor("r")
        src = 'await tools.exec_command({cmd: "git push origin main", yield_time_ms: 1000})'
        ex.feed(
            codex(
                "response_item",
                {"type": "custom_tool_call", "name": "exec", "call_id": "c2", "input": src},
            )
        )
        out = ex.feed(
            codex(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "c2",
                    "output": [{"type": "input_text", "text": "Process exited with code 1"}],
                },
            )
        )
        assert kinds(out) == ["error"]

    def test_apply_patch_path_and_turn_end(self):
        ex = CodexExtractor("r")
        patch = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch"
        events = ex.feed(
            codex(
                "response_item",
                {"type": "custom_tool_call", "name": "apply_patch", "call_id": "p", "input": patch},
            )
        )
        assert kinds(events) == [("tool", "write")]
        assert events[0].detail == {"path": "src/app.py"}
        assert kinds(ex.feed(codex("event_msg", {"type": "task_complete"}))) == ["turn_end"]


class TestLandingDetail:
    """Commits, pushes and PRs carry what a public link needs: repo, sha, url."""

    @pytest.fixture
    def repo(self, tmp_path):
        d = tmp_path / "wt"
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(
            ["git", "-C", str(d), "remote", "add", "origin", "git@github.com:gptme/gptme.git"],
            check=True,
        )
        github_slug.cache_clear()
        return d

    def test_cc_commit_in_cd_target(self, repo):
        ex = ClaudeCodeExtractor("s")
        ex.feed(
            {
                **cc_assistant([bash("c", f"cd {repo} && git commit -m x && git push")]),
                "cwd": "/somewhere/else",
            }
        )
        rec = cc_result("c")
        rec["message"]["content"][0]["content"] = [
            {"type": "text", "text": "[fix-1 1a2b3c4d] x\n 1 file changed"}
        ]
        commit, push = ex.feed(rec)
        assert commit.detail == {
            "cwd": "/somewhere/else",
            "dir": str(repo),
            "repo": "gptme/gptme",
            "sha": "1a2b3c4d",
        }
        assert push.detail["repo"] == "gptme/gptme" and "sha" not in push.detail

    def test_cc_commit_uses_record_cwd(self, repo):
        ex = ClaudeCodeExtractor("s")
        ex.feed({**cc_assistant([bash("c", "git commit -m x")]), "cwd": str(repo)})
        rec = cc_result("c")
        rec["message"]["content"][0]["content"] = "[main abcdef1] x"
        assert ex.feed(rec)[0].detail == {
            "cwd": str(repo),
            "dir": str(repo),
            "repo": "gptme/gptme",
            "sha": "abcdef1",
        }

    def test_pr_url_from_output(self):
        ex = GptmeExtractor("c")
        ex.feed(gptme_msg("assistant", '\n@shell(call_1): {"command": "gh pr create --fill"}'))
        (ev,) = ex.feed(
            gptme_msg("system", "https://github.com/gptme/gptme-contrib/pull/2117\n", "call_1")
        )
        assert ev.kind == "pr_open"
        assert ev.detail == {
            "repo": "gptme/gptme-contrib",
            "url": "https://github.com/gptme/gptme-contrib/pull/2117",
        }

    def test_unknown_repo_gives_no_detail(self, tmp_path):
        assert landing_detail("push", "git push", str(tmp_path), "") == {"dir": str(tmp_path)}
        assert github_slug("relative/path") is None


class TestTimestamps:
    def test_all_harnesses_share_one_utc_format(self):
        assert normalize_ts("2026-11-12T10:20:34.334Z") == "2026-11-12T10:20:34.334+00:00"
        assert normalize_ts("2026-11-12T11:20:34.334+01:00") == "2026-11-12T10:20:34.334+00:00"
        naive = normalize_ts("2026-11-12T07:30:13.266047")
        assert naive.endswith("+00:00") and len(naive) == len("2026-11-12T07:30:13.266+00:00")

    def test_bad_values_fall_back_to_now(self):
        assert normalize_ts(None).endswith("+00:00")
        assert normalize_ts("yesterday").endswith("+00:00")

    def test_events_carry_normalized_ts(self):
        ev = GptmeExtractor("c").feed(gptme_msg("assistant", "done"))[0]
        assert ev.ts == normalize_ts("2026-11-12T11:00:00")


class TestGptmeFailure:
    def test_hook_error_line_inside_output_is_not_a_failure(self):
        out = "Ran command: `git commit -m x`\n\n```stdout\nerror: hook warning\n[main abc1234] x\n```\n"
        assert not gptme_result_failed(out)

    def test_status_line_after_long_output_is_a_failure(self):
        out = "Ran command\n\n```stdout\n" + "x\n" * 3000 + "```\n\nReturn code: 1\n"
        assert gptme_result_failed(out)

    def test_tool_exception(self):
        assert gptme_result_failed("Error executing tool 'shell': boom")


def test_tool_events_carry_cwd_and_repo(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(
        ["git", "-C", str(d), "remote", "add", "origin", "https://github.com/o/r.git"], check=True
    )
    github_slug.cache_clear()
    ex = ClaudeCodeExtractor("s")
    (ev,) = ex.feed({**cc_assistant([bash("x", "ls")]), "cwd": str(d)})
    assert ev.detail == {"cwd": str(d), "repo": "o/r"}
