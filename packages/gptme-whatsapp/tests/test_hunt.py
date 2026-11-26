"""Bug-hunt regression tests for gptme-whatsapp setup helper."""

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from gptme_whatsapp import setup as wa_setup
from gptme_whatsapp.setup import NODE_DIR, generate_systemd_service


def _unit_value(service: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", service, re.MULTILINE)
    assert m, f"{key}= missing from unit"
    return m.group(1)


def test_node_dir_points_at_bridge():
    """NODE_DIR must be packages/gptme-whatsapp/node (where index.js lives)."""
    assert (NODE_DIR / "index.js").is_file(), f"NODE_DIR={NODE_DIR} has no index.js"


def test_generated_service_execstart_script_exists():
    service = generate_systemd_service("sven", "/home/sven/sven", ["447700900000"])
    # ExecStart follows shell-like quoting rules; WorkingDirectory= does not.
    script = Path(shlex.split(_unit_value(service, "ExecStart"))[-1])
    workdir = Path(_unit_value(service, "WorkingDirectory"))
    assert workdir.is_dir(), f"WorkingDirectory {workdir} does not exist"
    assert script.is_file(), f"ExecStart script {script} does not exist"


def test_bridge_path_with_space(monkeypatch, tmp_path):
    """ExecStart= needs quotes around a spaced path; WorkingDirectory= must not."""
    spaced = tmp_path / "my node"
    spaced.mkdir()
    (spaced / "index.js").write_text("// stub\n")
    monkeypatch.setattr(wa_setup, "NODE_DIR", spaced)
    service = generate_systemd_service("sven", "/home/sven/sven", ["447700900000"])
    assert _unit_value(service, "WorkingDirectory") == str(spaced)
    script = Path(shlex.split(_unit_value(service, "ExecStart"))[-1])
    assert script == spaced / "index.js"


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd is not installed"
)
def test_systemd_accepts_working_directory(monkeypatch, tmp_path):
    """Ask systemd itself: shlex cannot show how WorkingDirectory= is parsed."""
    spaced = tmp_path / "my 100% node"
    spaced.mkdir()
    monkeypatch.setattr(wa_setup, "NODE_DIR", spaced)
    unit = tmp_path / "gptme-whatsapp-test.service"
    unit.write_text(generate_systemd_service("sven", "/home/sven/sven", []))
    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Match only our directive: the exit code also covers host-specific checks
    # such as whether /usr/bin/node exists.
    assert "WorkingDirectory" not in result.stderr, result.stderr


def test_install_runs_npm_in_bridge_dir(monkeypatch):
    calls = []
    monkeypatch.setattr(wa_setup, "check_node", lambda: True)
    monkeypatch.setattr(wa_setup.shutil, "which", lambda name: f"/usr/bin/{name}")

    class R:
        returncode = 0

    def fake_run(cmd, cwd=None, **kw):
        calls.append(Path(cwd))
        return R()

    monkeypatch.setattr(wa_setup.subprocess, "run", fake_run)
    assert wa_setup.install_npm_deps()
    assert (calls[0] / "package.json").is_file(), f"npm install ran in {calls[0]}"


def test_workspace_with_space_survives_systemd_environment_parsing(tmp_path):
    """systemd splits Environment= on whitespace unless quoted.

    Verified with `systemd-analyze --user verify` on the generated unit:
    'Invalid environment assignment, ignoring: agent' -> AGENT_WORKSPACE=/.../my
    """
    workspace = str(tmp_path / "my agent")
    service = generate_systemd_service("sven", workspace, ["447700900000"])
    assigned = {}
    for line in service.splitlines():
        if line.startswith("Environment="):
            for word in shlex.split(line[len("Environment=") :]):
                k, _, v = word.partition("=")
                if _:
                    assigned[k] = v
    assert assigned.get("AGENT_WORKSPACE") == workspace


def test_percent_in_bridge_path_is_escaped_as_specifier(monkeypatch, tmp_path):
    """systemd expands %-specifiers in WorkingDirectory= and ExecStart=.

    A checkout under a directory named ``100%`` would otherwise make systemd
    try to expand ``%`` and fail the unit; it must render as ``%%``.
    """
    percent = tmp_path / "100%"
    percent.mkdir()
    (percent / "index.js").write_text("// stub\n")
    monkeypatch.setattr(wa_setup, "NODE_DIR", percent)
    service = generate_systemd_service("sven", "/home/sven/sven", ["447700900000"])
    for key in ("WorkingDirectory", "ExecStart"):
        value = _unit_value(service, key)
        assert "100%%" in value, f"{key} did not escape the percent: {value}"
        # No lone '%' left for systemd to expand as a specifier.
        assert not re.search(r"(?<!%)%(?!%)", value), value
