"""Exercise trace retention through real, provider-free child processes."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_activity_summary.cc_backend import _try_with_credential_file, call_claude_code
from gptme_activity_summary.gptme_backend import call_gptme


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_gptme_retains_child_trace(tmp_path, monkeypatch, caplog, outcome):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", "1")
    monkeypatch.setenv("TRACE_OUTCOME", outcome)
    marker = tmp_path / "child-path"
    monkeypatch.setenv("TRACE_MARKER", str(marker))
    child = tmp_path / "gptme"
    child.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['GPTME_LOGS_HOME'])\n"
        "Path(os.environ['TRACE_MARKER']).write_text(str(root))\n"
        "conversation = root / 'conversation'\n"
        "conversation.mkdir()\n"
        "(conversation / 'conversation.jsonl').write_text('retained trajectory\\n')\n"
        "(conversation / 'workspace').symlink_to(Path.cwd(), target_is_directory=True)\n"
        "print('diagnostic-' + 'x' * 500, file=sys.stderr, flush=True)\n"
        "print(json.dumps({'type': 'message', 'role': 'assistant', "
        "'content': '{\"narrative\": \"done\"}'}), flush=True)\n"
        "if os.environ['TRACE_OUTCOME'] == 'timeout': time.sleep(30)\n"
        "sys.exit(76 if os.environ['TRACE_OUTCOME'] == 'failure' else 0)\n"
    )
    child.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = call_gptme("test prompt", timeout=1 if outcome == "timeout" else 10)
    assert result == ('{"narrative": "done"}' if outcome == "success" else "")
    root = Path(marker.read_text())
    assert (root / "conversation/conversation.jsonl").read_text() == "retained trajectory\n"
    assert root.is_relative_to(tmp_path / "state")
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "conversation/workspace").is_symlink()
    assert "x" * 500 in (root / "stderr.log").read_text()
    assert '"role": "assistant"' in (root / "stdout.log").read_text()
    assert str(root) in caplog.text


@pytest.mark.parametrize("returncode", [0, 1, None])
def test_credential_slot_retains_trace(tmp_path, monkeypatch, caplog, returncode):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cred = tmp_path / "credentials.json"
    cred.write_text("{}")
    marker = tmp_path / "slot-path"
    code = (
        "import os, sys, time; from pathlib import Path; "
        "p = Path(os.environ['CLAUDE_CONFIG_DIR']); "
        f"Path({str(marker)!r}).write_text(str(p)); "
        "(p / 'projects').mkdir(); "
        "(p / 'projects/trace.jsonl').write_text('slot trajectory'); "
        + ("time.sleep(30)" if returncode is None else f"sys.exit({returncode})")
    )
    args = ([sys.executable, "-c", code], "prompt", cred, dict(os.environ), 1)
    if returncode is None:
        with pytest.raises(subprocess.TimeoutExpired):
            _try_with_credential_file(*args)
    else:
        result = _try_with_credential_file(*args)
        assert result.returncode == returncode
    root = Path(marker.read_text())
    assert (root / "projects/trace.jsonl").read_text() == "slot trajectory"
    assert root.is_relative_to(tmp_path / "state")
    assert not (root / ".credentials.json").is_symlink()
    assert cred.read_text() == "{}"
    assert str(root) in caplog.text


def test_claude_retries_keep_distinct_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    old_log = tmp_path / "claude-historical.log"
    old_log.write_text("historical diagnostic")
    os.utime(old_log, (1, 1))
    with patch("subprocess.run") as run, patch("time.sleep"):
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "transient"),
            subprocess.CompletedProcess([], 0, "answer", ""),
        ]
        assert call_claude_code("prompt", diagnostic_dir=tmp_path) == "answer"
    commands = [call.args[0] for call in run.call_args_list]
    assert all("--no-session-persistence" not in cmd for cmd in commands)
    ids = [cmd[cmd.index("--session-id") + 1] for cmd in commands]
    assert len(set(ids)) == 2
    assert old_log.read_text() == "historical diagnostic"


def test_trace_directory_default_and_isolation(tmp_path, monkeypatch):
    from gptme_activity_summary.traces import create_trace_dir

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    first = create_trace_dir("gptme-")
    second = create_trace_dir("gptme-")
    assert first != second
    assert first.parent == second.parent == tmp_path / ".local/state/gptme-activity-summary"
    assert first.is_dir() and second.is_dir()


def test_output_write_failure_preserves_response(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK", "1")
    with (
        patch("shutil.which", return_value="gptme"),
        patch("subprocess.run") as run,
        patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 0, '{"type":"message","role":"assistant","content":"answer"}', ""
        )
        assert call_gptme("prompt") == "answer"
    assert "Cannot save stdout.log" in caplog.text
    assert "Cannot save stderr.log" in caplog.text
