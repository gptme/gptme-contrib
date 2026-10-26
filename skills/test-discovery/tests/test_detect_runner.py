"""detect-runner.sh must agree with gptme's canonical completion-time detector."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "detect-runner.sh"
PYTEST = "uv run pytest -x -q" if shutil.which("uv") else "pytest -x -q"

CASES = {
    "pytest-ini": ({"pytest.ini": "[pytest]\n"}, PYTEST),
    "pyproject-pytest": (
        {"pyproject.toml": "[tool.pytest.ini_options]\nx = 1\n"},
        PYTEST,
    ),
    "pyproject-plain": ({"pyproject.toml": "[project]\nname = 'a'\n"}, None),
    "setup-cfg-pytest": ({"setup.cfg": "[tool:pytest]\nx = 1\n"}, PYTEST),
    "tox-with-pytest-section": (
        {"tox.ini": "[tox]\nenvlist = py\n[pytest]\nx = 1\n"},
        PYTEST,
    ),
    "tox": ({"tox.ini": "[tox]\nenvlist = py\n"}, "tox"),
    "tox-testenv-only": ({"tox.ini": "[testenv]\ncommands = x\n"}, None),
    "pyproject-tox": ({"pyproject.toml": "[tool.tox]\nx = 1\n"}, "tox"),
    "npm-test": ({"package.json": '{"scripts": {"test": "vitest run"}}'}, "npm test"),
    "npm-no-test-script": (
        {"package.json": '{"scripts": {"unit": "vitest run"}}'},
        None,
    ),
    "npm-bad-json": ({"package.json": "{"}, None),
    "cargo": ({"Cargo.toml": "[package]\nname = 'a'\n"}, "cargo test"),
    "make": ({"Makefile": "test:\n\ttrue\n"}, "make test"),
    "make-double-colon": ({"GNUmakefile": "test::\n\ttrue\n"}, "make test"),
    "make-no-test": ({"Makefile": "all:\n\ttrue\n"}, None),
    "loose-test-files": ({"test_x.py": "def test_x(): pass\n"}, None),
    "empty": ({}, None),
}


@pytest.mark.parametrize("files,expected", CASES.values(), ids=CASES.keys())
def test_detect_runner(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True
    )
    if expected is None:
        assert result.returncode == 1
        assert result.stdout == ""
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.split("  #")[0] == expected


def test_npm_lifecycle_scripts_shown(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"pretest": "echo pre", "test": "jest"}}'
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "npm lifecycle pretest: echo pre" in result.stderr
