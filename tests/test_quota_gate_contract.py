"""Contract tests for the quota-gate ⇄ check-claude-usage shell seam.

``quota-gate.sh`` is sourced (or executed) by every agent's autonomous run
script before starting a session. It shells out to ``check-claude-usage.sh
--json`` and reads a handful of *specific* top-level JSON keys
(``five_hour.utilization``, ``seven_day.utilization``,
``seven_day_sonnet.utilization``, ``_pacing`` …). Nothing today defends that
seam: if a rename in either file drifts the key names apart, the gate silently
reads ``0`` for every utilization and *never blocks* — the exact 3am failure
mode where an agent burns through its quota because the guard quietly became a
no-op.

These tests pin three things:

1. **Dual mode** — sourcing defines the function without running the gate;
   the run-on-execute guard only fires when executed directly.
2. **Exit contract** — 0 (proceed) below threshold, 1 (skip) at/over it, and
   fail-*open* (0) when the check script is missing or emits garbage, since a
   broken quota probe must never wedge the lane shut.
3. **Key binding** — the JSON keys quota-gate.sh reads are the same keys
   check-claude-usage.sh emits, checked statically so a rename in *either*
   file breaks this test rather than the 3am run.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
QUOTA_GATE = SCRIPTS / "quota-gate.sh"
CHECK_USAGE = SCRIPTS / "check-claude-usage.sh"

# The top-level keys the gate depends on. Kept here as the single written-down
# statement of the contract; both scripts are asserted against it below.
CONTRACT_KEYS = ("five_hour", "seven_day", "seven_day_sonnet")
UTIL_FIELD = "utilization"


def _run_gate(
    usage_json: str | None,
    *,
    model: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source quota-gate.sh, point it at a fake usage probe, call the function.

    ``usage_json=None`` simulates a missing check script (the probe path points
    at a file that does not exist).
    """
    with tempfile.TemporaryDirectory() as td:
        if usage_json is None:
            override = str(Path(td) / "does-not-exist.sh")
        else:
            fake = Path(td) / "check-claude-usage.sh"
            fake.write_text(
                "#!/usr/bin/env bash\ncat <<'JSON'\n" + usage_json + "\nJSON\n"
            )
            fake.chmod(0o755)
            override = str(fake)

        script = (
            f"source {shlex.quote(str(QUOTA_GATE))}\n"
            # Override the probe path *after* sourcing (the script derives it
            # from its own dir at source time; there is no env knob for it).
            f"QUOTA_CHECK_SCRIPT={shlex.quote(override)}\n"
            f"quota_gate_check {model}\n"
            "exit $?\n"
        )
        run_env = {"PATH": "/usr/bin:/bin"}
        if env:
            run_env.update(env)
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=run_env,
        )


def _usage(five: float, weekly: float, sonnet: float = 0.0) -> str:
    return (
        "{"
        f'"five_hour": {{"utilization": {five}, "time_left": "1h"}}, '
        f'"seven_day": {{"utilization": {weekly}, "time_left": "3d"}}, '
        f'"seven_day_sonnet": {{"utilization": {sonnet}, "time_left": "3d"}}, '
        '"_pacing": {"status": "on_track", "pace_gap": 0.0}'
        "}"
    )


# --- 1. Dual mode -----------------------------------------------------------


def test_sourcing_defines_function_without_running_gate() -> None:
    # Pure source: the function must exist and the gate must NOT have run (no
    # "quota OK" / "SKIPPING" log line, which the gate only emits when called).
    script = (
        f"source {shlex.quote(str(QUOTA_GATE))}\n"
        "declare -F quota_gate_check >/dev/null && echo DEFINED\n"
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert "DEFINED" in r.stdout
    # Sourcing alone must be side-effect free on both streams.
    assert "quota OK" not in r.stderr
    assert "SKIPPING" not in r.stderr


# --- 2. Exit contract -------------------------------------------------------


def test_below_threshold_proceeds() -> None:
    r = _run_gate(_usage(five=0.50, weekly=0.50))
    assert r.returncode == 0
    assert "quota OK" in r.stderr


def test_over_session_threshold_skips() -> None:
    r = _run_gate(_usage(five=0.95, weekly=0.50))
    assert r.returncode == 1
    assert "SKIPPING" in r.stderr
    assert "5h session" in r.stderr


def test_over_weekly_threshold_skips() -> None:
    r = _run_gate(_usage(five=0.10, weekly=0.95))
    assert r.returncode == 1
    assert "SKIPPING" in r.stderr
    assert "weekly" in r.stderr


def test_env_threshold_override_is_honoured() -> None:
    # A 0.50 five-hour utilization proceeds by default (0.90) but must skip
    # once the session threshold is lowered below it.
    r = _run_gate(
        _usage(five=0.50, weekly=0.10),
        env={"QUOTA_GATE_SESSION_THRESHOLD": "0.40"},
    )
    assert r.returncode == 1
    assert "SKIPPING" in r.stderr


def test_sonnet_reads_separate_weekly_counter() -> None:
    # For --model sonnet the weekly check must consult seven_day_sonnet, not
    # seven_day. seven_day is hot (0.95) but sonnet's counter is cold (0.10):
    # the gate must proceed.
    r = _run_gate(_usage(five=0.10, weekly=0.95, sonnet=0.10), model="sonnet")
    assert r.returncode == 0, r.stderr

    # And it must block when the sonnet counter itself is hot.
    r = _run_gate(_usage(five=0.10, weekly=0.10, sonnet=0.95), model="sonnet")
    assert r.returncode == 1, r.stderr


def test_missing_check_script_fails_open() -> None:
    r = _run_gate(None)
    assert r.returncode == 0
    assert "not found" in r.stderr


def test_malformed_json_fails_open() -> None:
    # A garbage probe response must never wedge the lane shut.
    r = _run_gate("this is not json")
    assert r.returncode == 0


# --- 3. Key binding (static) ------------------------------------------------


def test_gate_and_probe_agree_on_contract_keys() -> None:
    gate = QUOTA_GATE.read_text()
    probe = CHECK_USAGE.read_text()
    for key in CONTRACT_KEYS:
        assert key in gate, f"quota-gate.sh no longer reads {key!r}"
        assert key in probe, f"check-claude-usage.sh no longer emits {key!r}"
    # The nested field the gate divides on must exist on both sides too.
    assert UTIL_FIELD in gate
    assert UTIL_FIELD in probe
