"""Ensure a labelled CI test job cannot silently use another interpreter."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("matches", [True, False])
def test_runtime_check_happens_inside_pytest(tmp_path, matches):
    shutil.copyfile(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_example.py").write_text("def test_ok():\n    assert True\n")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    env = os.environ.copy()
    env["CI_EXPECTED_PYTHON"] = version if matches else "0.0"
    # An outer coverage/xdist invocation must not leak options into this replay.
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    if matches:
        assert result.returncode == 0, output
        assert "1 passed" in output
    else:
        assert result.returncode != 0
        assert "CI Python mismatch: expected 0.0" in output
        assert "1 passed" not in output


def test_workspace_matrix_is_explicit_and_bound_to_setup_python():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/test-packages.yml").read_text()
    )
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert job["env"]["CI_EXPECTED_PYTHON"] == "${{ matrix.python-version }}"
    assert job["env"]["UV_PYTHON_DOWNLOADS"] == "never"
    setup = next(step for step in job["steps"] if step.get("id") == "python")
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    install = next(
        step for step in job["steps"] if step.get("name") == "Install dependencies"
    )
    assert '--python "${{ steps.python.outputs.python-path }}"' in install["run"]
    tests = next(
        step
        for step in job["steps"]
        if step.get("name", "").startswith("Run tests for")
    )
    assert tests["env"]["UV_PYTHON"] == "${{ steps.python.outputs.python-path }}"
