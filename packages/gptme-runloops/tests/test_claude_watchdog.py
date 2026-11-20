"""Tests for the ``claude -p`` hang watchdog.

The session is a dummy Python child that replays a scripted stream-json log, so
no ``claude`` binary or network is needed. Thresholds are fractions of a second.
"""

from __future__ import annotations

import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from gptme_runloops import claude_watchdog
from gptme_runloops.claude_watchdog import (
    EXIT_HANG,
    StreamState,
    WatchdogConfig,
    run_watchdog,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX-only"
)

MODULE_PATH = Path(claude_watchdog.__file__)

# Replays a plan of steps: {"emit": event} prints one stream-json line,
# {"sleep": s} pauses, {"ignore_term": true} ignores SIGTERM,
# {"grandchild": pidfile} starts a long sleeper in the same process group,
# {"stubborn_grandchild": pidfile} starts one that ignores SIGTERM,
# {"bulk": n} prints n 1 KiB lines, {"close_stdout": true} closes stdout,
# {"exit": code} exits.
STUBBORN = (
    "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
)
DUMMY_SESSION = (
    "STUBBORN = "
    + repr(STUBBORN)
    + r"""
import json, os, signal, subprocess, sys, time
for step in json.loads(sys.argv[1]):
    if "emit" in step:
        sys.stdout.write(json.dumps(step["emit"]) + "\n")
        sys.stdout.flush()
    elif "raw" in step:
        sys.stdout.write(step["raw"] + "\n")
        sys.stdout.flush()
    elif "sleep" in step:
        time.sleep(step["sleep"])
    elif "ignore_term" in step:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif "grandchild" in step:
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
        )
        with open(step["grandchild"], "w") as f:
            f.write(str(p.pid))
    elif "stubborn_grandchild" in step:
        subprocess.Popen(
            [sys.executable, "-c", STUBBORN, step["stubborn_grandchild"]],
            stdout=subprocess.DEVNULL,
        )
        while not os.path.exists(step["stubborn_grandchild"]):
            time.sleep(0.01)
        time.sleep(0.05)
    elif "bulk" in step:
        for _ in range(step["bulk"]):
            sys.stdout.write("x" * 1023 + "\n")
        sys.stdout.flush()
    elif "close_stdout" in step:
        sys.stdout.close()
        os.close(1)
    elif "exit" in step:
        sys.exit(step["exit"])
"""
)

FAST = WatchdogConfig(
    idle_free=0.4, idle_tooled=2.0, grace=0.4, kill_after=0.5, poll=0.05
)


def init() -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": "s1"}


def text(msg: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": msg}]},
    }


def tool_use(uid: str, name: str = "Bash") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": {}}]
        },
    }


def tool_result(uid: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": uid, "content": "ok"}]
        },
    }


def result(is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "num_turns": 1,
    }


@pytest.fixture
def session_script(tmp_path: Path) -> Path:
    path = tmp_path / "dummy_session.py"
    path.write_text(DUMMY_SESSION)
    return path


def run(
    session_script: Path,
    tmp_path: Path,
    plan: list[Any],
    config: WatchdogConfig = FAST,
) -> tuple[int, dict[str, Any], bytes, float]:
    out_path = tmp_path / "stdout.jsonl"
    verdict_path = tmp_path / "verdict.jsonl"
    err = io.StringIO()
    start = time.monotonic()
    with out_path.open("wb") as out:
        code, verdict = run_watchdog(
            [sys.executable, str(session_script), json.dumps(plan)],
            config,
            verdict_file=str(verdict_path),
            out=out,
            err=err,
        )
    elapsed = time.monotonic() - start
    lines = verdict_path.read_text().splitlines()
    assert len(lines) == 1, "exactly one verdict line"
    assert json.loads(lines[0]) == verdict
    assert "[claude-watchdog] verdict " in err.getvalue()
    return code, verdict, out_path.read_bytes(), elapsed


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True
    # A zombie still answers kill(0); check its state where we can.
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
        return True
    return bool(state) and not state.startswith("Z")


def wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


# --- stream parsing ---------------------------------------------------------


def test_stream_state_tracks_tool_ids() -> None:
    state = StreamState()
    state.observe(json.dumps(tool_use("a", "Bash")), now=1.0)
    state.observe(json.dumps(tool_use("b", "Agent")).encode(), now=2.0)
    assert set(state.pending_tools) == {"a", "b"}
    # A result for an id we never saw (a subagent's inner call) is ignored.
    state.observe(json.dumps(tool_result("inner")), now=3.0)
    assert set(state.pending_tools) == {"a", "b"}
    state.observe(json.dumps(tool_result("a")), now=4.0)
    assert set(state.pending_tools) == {"b"}
    assert state.pending_summary(now=10.0) == [
        {"id": "b", "name": "Agent", "age_seconds": 8.0}
    ]
    state.observe("not json", now=5.0)
    state.observe("", now=5.0)
    assert state.events == 4
    state.observe(json.dumps(result()), now=6.0)
    assert state.result_seen and not state.result_is_error


def test_stream_state_error_result() -> None:
    state = StreamState()
    state.observe(json.dumps({"type": "result", "subtype": "error_max_turns"}), 0.0)
    assert state.result_seen and state.result_is_error


# --- end-to-end with a dummy session ------------------------------------------


def test_healthy_stream_does_not_trip(session_script: Path, tmp_path: Path) -> None:
    plan: list[dict[str, Any]] = [{"emit": init()}]
    for i in range(6):
        plan += [{"sleep": 0.15}, {"emit": text(f"step {i}")}]
    plan += [{"raw": "plain text line"}, {"emit": result()}, {"exit": 0}]

    code, verdict, out, elapsed = run(session_script, tmp_path, plan)

    # Total runtime (> 0.9 s) exceeds idle_free, but no single gap does.
    assert elapsed > FAST.idle_free
    assert code == 0
    assert verdict["outcome"] == "ok"
    assert verdict["trip"] is None
    assert verdict["result_seen"] is True
    # stdout is relayed unchanged, including non-JSON lines.
    lines = out.decode().splitlines()
    assert len(lines) == 9
    assert "plain text line" in lines
    assert json.loads(lines[-1])["type"] == "result"


def test_session_exit_code_passes_through(session_script: Path, tmp_path: Path) -> None:
    plan = [{"emit": init()}, {"exit": 3}]
    code, verdict, _, _ = run(session_script, tmp_path, plan)
    assert code == 3
    assert verdict["outcome"] == "error"
    assert verdict["trip"] is None
    assert verdict["child_returncode"] == 3


def test_silence_with_nothing_pending_trips(
    session_script: Path, tmp_path: Path
) -> None:
    plan = [{"emit": init()}, {"emit": text("thinking")}, {"sleep": 30}]
    code, verdict, _, elapsed = run(session_script, tmp_path, plan)

    assert code == EXIT_HANG
    assert verdict["outcome"] == "hang"
    assert verdict["trip"] == "idle"
    assert verdict["pending_tools"] == []
    assert verdict["idle_seconds"] >= FAST.idle_free
    assert verdict["child_returncode"] == -signal.SIGTERM
    assert verdict["escalated_to_sigkill"] is False
    assert elapsed < FAST.idle_tooled  # tripped on the short limit


def test_pending_tool_gets_longer_allowance(
    session_script: Path, tmp_path: Path
) -> None:
    # The tool runs silently for 2x idle_free, then returns; no trip.
    plan = [
        {"emit": init()},
        {"emit": tool_use("toolu_1")},
        {"sleep": FAST.idle_free * 2},
        {"emit": tool_result("toolu_1")},
        {"emit": result()},
        {"exit": 0},
    ]
    code, verdict, _, _ = run(session_script, tmp_path, plan)
    assert code == 0
    assert verdict["outcome"] == "ok"
    assert verdict["trip"] is None


def test_pending_tool_over_allowance_trips(
    session_script: Path, tmp_path: Path
) -> None:
    config = WatchdogConfig(
        idle_free=0.3, idle_tooled=1.0, grace=0.3, kill_after=0.5, poll=0.05
    )
    plan = [
        {"emit": init()},
        {"emit": tool_use("toolu_parent", "Agent")},
        # A subagent's inner result must not clear the parent's pending call.
        {"emit": tool_result("toolu_inner")},
        {"sleep": 30},
    ]
    code, verdict, _, elapsed = run(session_script, tmp_path, plan, config)

    assert code == EXIT_HANG
    assert verdict["outcome"] == "hang"
    assert verdict["trip"] == "tool_pending"
    assert [p["id"] for p in verdict["pending_tools"]] == ["toolu_parent"]
    assert verdict["pending_tools"][0]["name"] == "Agent"
    assert verdict["idle_seconds"] >= config.idle_tooled
    assert elapsed >= config.idle_tooled


def test_post_result_hang_is_killed_within_grace(
    session_script: Path, tmp_path: Path
) -> None:
    plan = [{"emit": init()}, {"emit": result()}, {"sleep": 30}]
    code, verdict, _, elapsed = run(session_script, tmp_path, plan)

    # The work finished; only the exit hung, so the exit code is success.
    assert code == 0
    assert verdict["outcome"] == "ok"
    assert verdict["trip"] == "post_result"
    assert elapsed < FAST.grace + 5


def test_post_result_hang_after_error_result(
    session_script: Path, tmp_path: Path
) -> None:
    plan = [{"emit": init()}, {"emit": result(is_error=True)}, {"sleep": 30}]
    code, verdict, _, _ = run(session_script, tmp_path, plan)
    assert code == 1
    assert verdict["outcome"] == "error"
    assert verdict["trip"] == "post_result"


def test_sigterm_ignored_escalates_to_sigkill(
    session_script: Path, tmp_path: Path
) -> None:
    plan = [{"ignore_term": True}, {"emit": init()}, {"sleep": 30}]
    code, verdict, _, elapsed = run(session_script, tmp_path, plan)

    assert code == EXIT_HANG
    assert verdict["escalated_to_sigkill"] is True
    assert verdict["child_returncode"] == -signal.SIGKILL
    # SIGKILL came after the SIGTERM grace, not before.
    assert elapsed >= FAST.idle_free + FAST.kill_after


def test_trip_stops_the_whole_process_group(
    session_script: Path, tmp_path: Path
) -> None:
    pidfile = tmp_path / "grandchild.pid"
    plan = [{"grandchild": str(pidfile)}, {"emit": init()}, {"sleep": 30}]
    code, verdict, _, _ = run(session_script, tmp_path, plan)

    assert code == EXIT_HANG
    assert verdict["escalated_to_sigkill"] is False
    assert wait_dead(int(pidfile.read_text())), "tool subprocess survived"


def test_sigterm_resistant_tool_is_killed_after_leader_exits(
    session_script: Path, tmp_path: Path
) -> None:
    # The leader exits on SIGTERM at once; a tool process in its group ignores it.
    pidfile = tmp_path / "stubborn.pid"
    plan = [{"stubborn_grandchild": str(pidfile)}, {"emit": init()}, {"sleep": 30}]
    code, verdict, _, _ = run(session_script, tmp_path, plan)

    assert code == EXIT_HANG
    assert verdict["escalated_to_sigkill"] is True
    assert wait_dead(int(pidfile.read_text())), "SIGTERM-resistant tool survived"


def test_eof_after_result_keeps_the_result_deadline(
    session_script: Path, tmp_path: Path
) -> None:
    # Closing stdout just before the grace ends must not start a new grace period.
    config = WatchdogConfig(
        idle_free=30.0, idle_tooled=30.0, grace=2.0, kill_after=0.5, poll=0.05
    )
    plan = [
        {"emit": init()},
        {"emit": result()},
        {"sleep": 1.8},
        {"close_stdout": True},
        {"sleep": 30},
    ]
    code, verdict, _, elapsed = run(session_script, tmp_path, plan, config)

    assert code == 0
    assert verdict["trip"] == "post_result"
    # Result deadline at ~2.0s; a restarted grace would end at ~3.8s.
    assert elapsed < 3.0, elapsed


def test_stalled_downstream_reader_does_not_stop_supervision(
    session_script: Path, tmp_path: Path
) -> None:
    # A pipe nobody reads: far more output than the pipe buffer holds.
    read_fd, write_fd = os.pipe()
    plan = [{"emit": init()}, {"bulk": 512}, {"sleep": 30}]
    box: dict[str, Any] = {}

    def target() -> None:
        with os.fdopen(write_fd, "wb", closefd=False) as out:
            box["result"] = run_watchdog(
                [sys.executable, str(session_script), json.dumps(plan)],
                FAST,
                out=out,
                err=io.StringIO(),
            )

    thread = threading.Thread(target=target, daemon=True)
    try:
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "watchdog blocked on a full output pipe"
        code, verdict = box["result"]
        assert code == EXIT_HANG
        assert verdict["trip"] == "idle"
    finally:
        os.close(read_fd)  # unblocks any writer still stuck on the pipe
        thread.join(timeout=10)
        os.close(write_fd)


class _LateQueue:
    """Line queue that hands an item out only once it is ``lag`` seconds old.

    Simulates output that is already buffered while the supervision loop has
    not consumed it yet (a ``get`` that timed out just before a line landed).
    """

    def __init__(self, lag: float) -> None:
        self._lag = lag
        self._items: list[tuple[float, Any]] = []
        self._lock = threading.Lock()

    def put(self, item: Any) -> None:
        with self._lock:
            self._items.append((time.monotonic(), item))

    def empty(self) -> bool:
        with self._lock:
            return not self._items

    def get_nowait(self) -> Any:
        with self._lock:
            if not self._items:
                raise queue.Empty
            return self._items.pop(0)[1]

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        deadline = time.monotonic() + (timeout or 0.0)
        while True:
            with self._lock:
                if self._items and time.monotonic() - self._items[0][0] >= self._lag:
                    return self._items.pop(0)[1]
            if time.monotonic() >= deadline:
                raise queue.Empty
            time.sleep(0.005)


def test_buffered_unread_output_blocks_idle_trip(
    session_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lines reach the watchdog's queue every 0.2 s, but each one is consumed
    # only after it has waited longer than idle_free. The session is never
    # silent, so unread output in the queue must hold off the idle trip.
    config = WatchdogConfig(
        idle_free=1.0, idle_tooled=5.0, grace=1.0, kill_after=0.5, poll=0.05
    )
    made: list[Any] = []

    def make_queue() -> Any:
        q: Any = _LateQueue(lag=1.5) if not made else queue.Queue()
        made.append(q)
        return q

    monkeypatch.setattr(
        claude_watchdog,
        "queue",
        types.SimpleNamespace(Queue=make_queue, Empty=queue.Empty),
    )
    plan: list[dict[str, Any]] = [{"emit": init()}]
    for i in range(12):
        plan += [{"sleep": 0.2}, {"emit": text(f"step {i}")}]
    plan += [{"emit": result()}, {"exit": 0}]

    code, verdict, out, _ = run(session_script, tmp_path, plan, config)

    assert isinstance(made[0], _LateQueue)
    assert verdict["trip"] is None, verdict["reason"]
    assert code == 0
    assert len(out.decode().splitlines()) == 14


# --- command line -------------------------------------------------------------


def _script_cmd(*args: str) -> list[str]:
    # Run the module file directly: it is stdlib-only and must work standalone.
    return [sys.executable, str(MODULE_PATH), *args]


def test_cli_env_thresholds_and_verdict_file(
    session_script: Path, tmp_path: Path
) -> None:
    verdict_path = tmp_path / "verdict.jsonl"
    env = {
        **os.environ,
        "AGENT_CLAUDE_WATCHDOG_IDLE_FREE": "0.3",
        "AGENT_CLAUDE_WATCHDOG_KILL_AFTER": "0.5",
        "AGENT_CLAUDE_WATCHDOG_POLL": "0.05",
    }
    plan = [{"emit": init()}, {"sleep": 30}]
    proc = subprocess.run(
        _script_cmd(
            "--verdict-file",
            str(verdict_path),
            "--",
            sys.executable,
            str(session_script),
            json.dumps(plan),
        ),
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == EXIT_HANG, proc.stderr
    assert json.loads(proc.stdout.decode().splitlines()[0])["type"] == "system"
    verdict = json.loads(verdict_path.read_text())
    assert verdict["outcome"] == "hang"
    assert verdict["trip"] == "idle"


def test_cli_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CLAUDE_WATCHDOG_IDLE_FREE", "7")
    monkeypatch.setenv("AGENT_CLAUDE_WATCHDOG_IDLE_TOOLED", "8")
    monkeypatch.delenv("AGENT_CLAUDE_WATCHDOG_GRACE", raising=False)
    args = claude_watchdog.build_parser().parse_args(
        ["--idle-tooled", "9", "--", "claude"]
    )
    assert args.idle_free == 7.0
    assert args.idle_tooled == 9.0
    assert args.grace == claude_watchdog.GRACE_DEFAULT
    assert args.cmd == ["--", "claude"]


def test_cli_usage_errors() -> None:
    proc = subprocess.run(_script_cmd(), capture_output=True, timeout=15)
    assert proc.returncode == 2
    proc = subprocess.run(
        _script_cmd("--idle-free", "0", "--", "true"), capture_output=True, timeout=15
    )
    assert proc.returncode == 2


def test_cli_signal_stops_session_group(session_script: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    verdict_path = tmp_path / "verdict.jsonl"
    plan = [{"grandchild": str(pidfile)}, {"emit": init()}, {"sleep": 30}]
    proc = subprocess.Popen(
        _script_cmd(
            "--verdict-file",
            str(verdict_path),
            "--poll",
            "0.05",
            "--kill-after",
            "0.5",
            "--",
            sys.executable,
            str(session_script),
            json.dumps(plan),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists()
        time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.wait()
    assert proc.returncode == 128 + signal.SIGTERM
    assert json.loads(verdict_path.read_text())["outcome"] == "interrupted"
    assert wait_dead(int(pidfile.read_text())), "session survived the watchdog"
