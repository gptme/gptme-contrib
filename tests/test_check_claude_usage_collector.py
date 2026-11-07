"""Hermetic collector tests for check-claude-usage.sh.

The collector samples a tmux /usage pane, accumulates frames, and stops
once it has seen enough window labels. These tests fake tmux (and sleep)
so they exercise that loop without launching Claude Code.

They pin three production failure modes on CC 2.1.318+ weekly-only panes:

1. ``echo "$ACCUM" | grep`` on a large capture SIGPIPEs under ``pipefail``
   (Alice's broken-pipe report).
2. Requiring two of {session, weekly-all, weekly-sonnet} never completes on
   a weekly-only pane, so the loop exhausts all 12 tab cycles.
3. Optional ``Current week (<model>)`` labels (Fable, Sonnet) must not be
   confused with the all-models weekly window, and must not force extra
   cycles once weekly-all is captured.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-claude-usage.sh"
FIXTURE = REPO / "tests" / "fixtures" / "cc-usage" / "usage-pane-2.1.319.pane"

WEEKLY_ONLY_PANE = textwrap.dedent(
    """\
    shortcuts
    Esc to cancel
    d to day
    w to week

    Session

    Total cost:            $0.0000
    Total duration (API):  0s
    Usage:                 0 input, 0 output, 0 cache read, 0 cache write

    Current week (all models)
    ████▌                                              9% used
    Resets Nov 13, 8am (UTC)

    Current week (Fable)
                                                       0% used
    Resets Nov 13, 8am (UTC)
    """
)

LEGACY_SESSION_WEEKLY_PANE = textwrap.dedent(
    """\
    shortcuts
    Esc to cancel
    d to day
    w to week
    Current session
    ██  11% used
    Resets 2pm (UTC)
    Current week (all models)
    ████  33% used
    Resets Jun 12, 8am (UTC)
    Current week (Sonnet only)
    ██  19% used
    Resets Jun 12, 7:59am (UTC)
    """
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_home(tmp_path: Path, payload: str, *, delayed_payload: str = "") -> Path:
    """Install no-op sleep + a tmux stub that serves ``payload`` after init."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}\n")

    payload_file = tmp_path / "payload.txt"
    payload_file.write_text(payload)
    delayed_file = tmp_path / "delayed-payload.txt"
    delayed_file.write_text(delayed_payload)
    counter_file = tmp_path / "capture-count.txt"
    right_file = tmp_path / "right-count.txt"
    counter_file.write_text("0")
    right_file.write_text("0")

    _write_executable(
        bin_dir / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        bin_dir / "tmux",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            counter_file={shlex.quote(str(counter_file))}
            payload_file={shlex.quote(str(payload_file))}
            right_file={shlex.quote(str(right_file))}
            cmd="${{1:-}}"
            shift || true
            case "$cmd" in
                list-sessions) exit 0 ;;
                new-session|kill-session) exit 0 ;;
                send-keys)
                    for arg in "$@"; do
                        if [ "$arg" = "Right" ]; then
                            n=0
                            [ -f "$right_file" ] && n=$(cat "$right_file")
                            printf '%s' "$((n + 1))" > "$right_file"
                        fi
                    done
                    exit 0
                    ;;
                capture-pane)
                    count=0
                    [ -f "$counter_file" ] && count=$(cat "$counter_file")
                    printf '%s' "$((count + 1))" > "$counter_file"
                    if [ "$count" -eq 0 ]; then
                        printf 'shortcuts\\n'
                    else
                        cat "$payload_file"
                        if [ "$(cat "$right_file")" -ge 2 ]; then
                            cat {shlex.quote(str(delayed_file))}
                        fi
                    fi
                    ;;
                *)
                    echo "unexpected tmux command: $cmd" >&2
                    exit 1
                    ;;
            esac
            """
        ),
    )
    return home


def _run_collector(
    tmp_path: Path,
    payload: str,
    *args: str,
    timeout: int = 20,
    delayed_payload: str = "",
) -> subprocess.CompletedProcess[str]:
    home = _fake_home(tmp_path, payload, delayed_payload=delayed_payload)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{home / '.local' / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}",
            "CLAUDE_USAGE_CACHE_FILE": str(tmp_path / "usage-cache.json"),
            "CLAUDE_USAGE_SCRAPE_LOCK": str(tmp_path / "scrape.lock"),
            "CLAUDE_USAGE_CREDS_FILE": str(home / ".claude" / ".credentials.json"),
        }
    )
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)
    return subprocess.run(
        [str(SCRIPT), *args],
        text=True,
        capture_output=True,
        cwd=str(REPO),
        env=env,
        timeout=timeout,
    )


def _right_count(tmp_path: Path) -> int:
    path = tmp_path / "right-count.txt"
    return int(path.read_text()) if path.exists() else 0


def test_weekly_only_large_capture_avoids_epipe_and_exhausting_cycles(
    tmp_path: Path,
) -> None:
    """A large weekly-only pane must return weekly numbers without EPIPE.

    Repeating the sanitized CC 2.1.319 fixture inflates ACCUM past PIPE_BUF
    so ``echo "$ACCUM" | grep`` SIGPIPEs under pipefail. The collector must
    still stop once the all-models weekly bar is captured — optional Fable
    must not force all 12 tab cycles.
    """
    fixture = FIXTURE.read_text()
    # Keep the wait-loop tokens plus a bulky repeating body.
    bulky = (WEEKLY_ONLY_PANE + "\n" + fixture + "\n") * 40

    result = _run_collector(tmp_path, bulky, "--raw", "--no-cache")

    stderr = result.stderr.lower()
    assert result.returncode == 0, result.stderr
    assert "broken pipe" not in stderr
    assert "epipe" not in stderr
    assert "current week (all models)" in result.stdout.lower()
    assert "9% used" in result.stdout
    # Session cost summary may appear; the 5h quota *label* must not.
    assert "current session" not in result.stdout.lower()
    rights = _right_count(tmp_path)
    assert rights < 12, f"exhausted tab cycles: {rights} Right keys"
    assert rights >= 1, "never entered the capture loop"


def test_legacy_session_weekly_layout_retains_session_utilization(
    tmp_path: Path,
) -> None:
    """Old session+weekly panes must still parse five_hour and stop early."""
    result = _run_collector(
        tmp_path, LEGACY_SESSION_WEEKLY_PANE, "--json", "--no-cache"
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["five_hour"]["utilization"] == 0.11
    assert parsed["seven_day"]["utilization"] == 0.33
    assert parsed["seven_day_sonnet"]["utilization"] == 0.19
    rights = _right_count(tmp_path)
    assert rights < 12, f"legacy layout exhausted cycles: {rights}"


def test_weekly_only_captures_sonnet_on_next_render(tmp_path: Path) -> None:
    """Weekly-all alone must allow a bounded follow-up for late model bars."""
    result = _run_collector(
        tmp_path,
        "shortcuts\nCurrent week (all models)\n33% used\n",
        "--raw",
        "--no-cache",
        delayed_payload="Current week (Sonnet only)\n95% used\n",
    )
    assert result.returncode == 0, result.stderr
    assert "Current week (Sonnet only)" in result.stdout
    assert "95% used" in result.stdout
    assert _right_count(tmp_path) == 2
