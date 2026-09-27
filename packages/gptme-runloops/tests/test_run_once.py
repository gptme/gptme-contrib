"""Tests for one-shot, resumable runs (Executor.run_once + the `run` CLI).

The backend CLIs are mocked at subprocess.run; tests assert the argv plumbing
per backend (resume, model, tool restriction) and the result parsing.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from gptme_runloops.cli import main
from gptme_runloops.utils import run_once as ro
from gptme_runloops.utils.executor import (
    ClaudeCodeExecutor,
    CodexExecutor,
    GptmeExecutor,
    GrokBuildExecutor,
    get_executor,
    list_backends,
)
from gptme_runloops.utils.run_once import ResumeNotSupportedError, RunOnceResult

WS = Path("/tmp")
SID = "1403ce9d-3940-4716-9ddf-f22e2e3f824a"


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def _argv(mock_run: MagicMock) -> list[str]:
    return mock_run.call_args[0][0]


# --- claude-code ---


def _cc_json(**over) -> str:
    out = {
        "result": "done",
        "session_id": SID,
        "is_error": False,
        "total_cost_usd": 0.12,
        "modelUsage": {
            "claude-haiku-4-5": {"outputTokens": 10},
            "claude-opus-4-8": {"outputTokens": 900},
        },
    }
    out.update(over)
    return json.dumps(out)


def test_claude_fresh_with_allowlist():
    with patch.object(ro.subprocess, "run", return_value=_proc(_cc_json())) as run:
        r = ClaudeCodeExecutor().run_once(
            "hi", WS, 60, model="opus", allowed_tools=["Read", "Bash(ls:*)"]
        )
    argv = _argv(run)
    assert argv[:3] == ["claude", "-p", "hi"]
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1 :][:2] == ["Read", "Bash(ls:*)"]
    assert "--dangerously-skip-permissions" not in argv
    assert r.session_id == SID
    assert r.model == "claude-opus-4-8"  # main model, not the subagent's
    assert r.result == "done" and not r.is_error and r.agent_output
    assert r.cost_usd == 0.12 and r.resumed is False


def test_claude_resume_and_default_permissions():
    with patch.object(ro.subprocess, "run", return_value=_proc(_cc_json())) as run:
        r = ClaudeCodeExecutor().run_once("more", WS, 60, resume=SID)
    argv = _argv(run)
    assert argv[argv.index("--resume") + 1] == SID
    assert "--dangerously-skip-permissions" in argv
    assert r.resumed is True


def test_claude_rejected_resume_has_no_agent_output():
    proc = _proc("", "No conversation found with session ID", returncode=1)
    with patch.object(ro.subprocess, "run", return_value=proc):
        r = ClaudeCodeExecutor().run_once("more", WS, 60, resume=SID)
    assert r.is_error and not r.agent_output
    assert "No conversation found" in r.result


def test_claude_strips_nesting_env(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(ro.subprocess, "run", return_value=_proc(_cc_json())) as run:
        ClaudeCodeExecutor().run_once("hi", WS, 60, env={"X": "1"})
    env = run.call_args.kwargs["env"]
    assert "CLAUDECODE" not in env and env["X"] == "1"
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_claude_rejects_sandbox():
    with pytest.raises(ValueError):
        ClaudeCodeExecutor().run_once("hi", WS, 60, sandbox="read-only")


# --- codex ---


def _codex_side_effect(thread_id: str, text: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        if text:
            out.write_text(text + "\n")
        events = [
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "turn.completed", "usage": {}},
        ]
        return _proc("\n".join(json.dumps(e) for e in events), "", returncode)

    return fake_run


def test_codex_fresh():
    with patch.object(
        ro.subprocess, "run", side_effect=_codex_side_effect(SID, "pong")
    ) as run:
        r = CodexExecutor().run_once("ping", WS, 60, model="gpt-5.5")
    argv = _argv(run)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "resume" not in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(WS)
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert argv[-1] == "ping"
    assert r.session_id == SID and r.result == "pong" and not r.is_error
    assert r.model == "gpt-5.5"


def test_codex_resume_uses_config_sandbox():
    with patch.object(
        ro.subprocess, "run", side_effect=_codex_side_effect(SID, "again")
    ) as run:
        r = CodexExecutor().run_once("more", WS, 60, resume=SID, sandbox="read-only")
    argv = _argv(run)
    assert argv[:4] == ["codex", "exec", "resume", "--json"]
    assert 'sandbox_mode="read-only"' in argv
    assert "-s" not in argv and "-C" not in argv
    assert argv[-2:] == [SID, "more"]
    assert r.resumed and r.result == "again"


def test_codex_rejects_allowlist_and_bad_sandbox():
    with pytest.raises(ValueError, match="allowlist"):
        CodexExecutor().run_once("x", WS, 60, allowed_tools=["Read"])
    with pytest.raises(ValueError, match="sandbox"):
        CodexExecutor().run_once("x", WS, 60, sandbox="yolo")


def test_codex_no_message_is_error():
    with patch.object(
        ro.subprocess, "run", side_effect=_codex_side_effect(SID, "", returncode=1)
    ):
        r = CodexExecutor().run_once("x", WS, 60, resume=SID)
    assert r.is_error and not r.agent_output


# --- gptme ---


def _gptme_stdout(*contents: str, model: str = "openrouter/x/y") -> str:
    lines = [{"type": "message", "role": "user", "content": "hi"}]
    for c in contents:
        lines.append(
            {
                "type": "message",
                "role": "assistant",
                "content": c,
                "metadata": {"model": model, "cost": 0.1},
            }
        )
    return "\n".join(json.dumps(x) for x in lines)


def test_gptme_fresh_names_session(tmp_path, monkeypatch):
    monkeypatch.setenv("GPTME_LOGS_HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        (tmp_path / cmd[cmd.index("--name") + 1]).mkdir()
        return _proc(_gptme_stdout("answer", "<think>x</think>\n@complete(id): {}"))

    with patch.object(ro.subprocess, "run", side_effect=fake_run) as run:
        r = GptmeExecutor().run_once(
            "q", WS, 60, model="m/x", allowed_tools=["read", "save"]
        )
    argv = _argv(run)
    name = argv[argv.index("--name") + 1]
    assert name.startswith("run-once-")
    assert argv[argv.index("--tools") + 1] == "read,save"
    assert argv[argv.index("--model") + 1] == "m/x"
    assert "--non-interactive" in argv and argv[-1] == "q"
    assert r.session_id == name
    assert r.result == "answer"  # tool-call-only final message skipped
    assert r.model == "openrouter/x/y"
    assert r.cost_usd == pytest.approx(0.2)


def test_gptme_resume_reuses_name(tmp_path, monkeypatch):
    monkeypatch.setenv("GPTME_LOGS_HOME", str(tmp_path))
    (tmp_path / "run-once-abc").mkdir()
    with patch.object(
        ro.subprocess, "run", return_value=_proc(_gptme_stdout("ok"))
    ) as run:
        r = GptmeExecutor().run_once("more", WS, 60, resume="run-once-abc")
    argv = _argv(run)
    assert argv[argv.index("--name") + 1] == "run-once-abc"
    assert r.session_id == "run-once-abc" and r.resumed


def test_gptme_unpersisted_session_not_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("GPTME_LOGS_HOME", str(tmp_path))
    with patch.object(ro.subprocess, "run", return_value=_proc(_gptme_stdout("ok"))):
        r = GptmeExecutor().run_once("q", WS, 60)
    assert r.session_id is None and r.result == "ok"


# --- shared behavior ---


def test_timeout_result():
    with patch.object(
        ro.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 5)
    ):
        r = ClaudeCodeExecutor().run_once("x", WS, 5, resume=SID)
    assert r.timed_out and r.is_error and r.exit_code == 124
    assert not r.agent_output and r.session_id == SID


def test_grok_build_cannot_resume_or_run_once():
    with pytest.raises(ResumeNotSupportedError):
        GrokBuildExecutor().run_once("x", WS, 60, resume=SID)
    with pytest.raises(NotImplementedError):
        GrokBuildExecutor().run_once("x", WS, 60)


def test_codex_registered():
    assert "codex" in list_backends()
    with patch("shutil.which", return_value="/usr/bin/codex"):
        assert isinstance(get_executor("codex"), CodexExecutor)


def test_codex_execute_uses_run_once():
    res = RunOnceResult("codex", None, SID, "out", 0)
    with patch.object(CodexExecutor, "run_once", return_value=res) as once:
        er = CodexExecutor().execute("p", WS, 60, model="gpt-5.5")
    assert er.exit_code == 0
    assert once.call_args.kwargs["model"] == "gpt-5.5"


# --- `run` CLI ---


def _cli(args: list[str], results: list[RunOnceResult]):
    with (
        patch("shutil.which", return_value="/usr/bin/x"),
        patch(
            "gptme_runloops.utils.executor.Executor.run_once", side_effect=results
        ) as once,
    ):
        res = CliRunner().invoke(main, ["run", "--workspace", "/tmp", *args])
    return res, once


def test_cli_run_prints_json_and_passes_options():
    ok = RunOnceResult("claude-code", "claude-opus-4-8", SID, "hi", 0)
    res, once = _cli(
        [
            "--backend",
            "claude-code",
            "--model",
            "opus",
            "--allowed-tool",
            "Read",
            "--allowed-tool",
            "Edit",
            "prompt",
        ],
        [ok],
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["session_id"] == SID and out["model"] == "claude-opus-4-8"
    assert out["is_error"] is False
    kw = once.call_args.kwargs
    assert kw["model"] == "opus" and kw["allowed_tools"] == ["Read", "Edit"]
    assert kw["resume"] is None


def test_cli_resume_failure_errors_by_default():
    rejected = RunOnceResult(
        "codex", None, SID, "no such thread", 1, True, agent_output=False
    )
    res, once = _cli(["--backend", "codex", "--resume", SID, "more"], [rejected])
    assert res.exit_code == 1
    assert json.loads(res.output)["is_error"] is True
    assert once.call_count == 1


def test_cli_resume_failure_fresh_fallback(tmp_path):
    rejected = RunOnceResult(
        "codex", None, SID, "no such thread", 1, True, agent_output=False
    )
    fresh = RunOnceResult("codex", None, "new-id", "done", 0)
    fb = tmp_path / "fb.txt"
    fb.write_text("context + more")
    res, once = _cli(
        [
            "--backend",
            "codex",
            "--resume",
            SID,
            "--on-resume-failure",
            "fresh",
            "--fallback-prompt-file",
            str(fb),
            "more",
        ],
        [rejected, fresh],
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["resume_failed"] is True and out["session_id"] == "new-id"
    assert once.call_args_list[1].args[0] == "context + more"
    assert once.call_args_list[1].kwargs["resume"] is None


def test_cli_no_fallback_when_resumed_session_ran():
    # The session ran and reported an error: never re-run it fresh.
    ran = RunOnceResult("claude-code", None, SID, "tool denied", 1, True)
    res, once = _cli(
        [
            "--backend",
            "claude-code",
            "--resume",
            SID,
            "--on-resume-failure",
            "fresh",
            "x",
        ],
        [ran],
    )
    assert res.exit_code == 1 and once.call_count == 1


def test_cli_unsupported_resume_is_usage_error():
    with patch(
        "gptme_runloops.utils.executor._resolve_grok_build_binary", return_value="/x"
    ):
        res = CliRunner().invoke(
            main,
            [
                "run",
                "--workspace",
                "/tmp",
                "--backend",
                "grok-build",
                "--resume",
                SID,
                "x",
            ],
        )
    assert res.exit_code == 2
    assert "resume is not supported" in res.output


def test_cli_backend_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    ok = RunOnceResult("codex", None, SID, "hi", 0)
    with (
        patch("shutil.which", return_value="/usr/bin/x"),
        patch.object(CodexExecutor, "run_once", return_value=ok) as once,
    ):
        res = CliRunner().invoke(main, ["run", "--workspace", "/tmp", "p"])
    assert res.exit_code == 0, res.output
    assert once.called


def test_gptme_visible_text_strips_markdown_tool_blocks():
    msg = "Here you go.\n```save out.txt\ncontent\n```\n```python\nx = 1\n```\n```complete\n```"
    assert ro.gptme_visible_text(msg) == "Here you go.\n\n```python\nx = 1\n```"
    assert ro.gptme_visible_text("```complete\n```") == ""


def test_codex_model_falls_back_to_config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('model = "gpt-9"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with patch.object(ro.subprocess, "run", side_effect=_codex_side_effect(SID, "ok")):
        r = CodexExecutor().run_once("x", WS, 60)
    assert r.model == "gpt-9"
