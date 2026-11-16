"""The suite must never inherit a real workspace from the caller's env."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_isolation_clears_inherited_workspace_env(tmp_path: Path) -> None:
    # Exercise the real conftest in a fresh process, even on clean CI runners.
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(
        "import os\n"
        "def test_no_workspace_env():\n"
        "    assert 'GPTODO_TASKS_DIR' not in os.environ\n"
        "    assert 'TASKS_REPO_ROOT' not in os.environ\n"
    )
    decoy = tmp_path / "workspace"
    decoy.mkdir()
    env = {
        **os.environ,
        "GPTODO_TASKS_DIR": str(decoy),
        "TASKS_REPO_ROOT": str(decoy),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_probe.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(decoy.iterdir())


def test_workspace_env_is_isolated() -> None:
    # Mirrors conftest.WORKSPACE_ENV_VARS; the autouse fixture must clear both.
    for name in ("GPTODO_TASKS_DIR", "TASKS_REPO_ROOT"):
        assert name not in os.environ, f"{name} leaked into the gptodo tests"
