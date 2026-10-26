"""Pin canonical discovery behavior and documented compatibility corrections."""

import os
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
    "tox-testenv-only": (
        {"tox.ini": "[testenv]\ncommands = python -m unittest discover\n"},
        "tox",
    ),
    "tox-named-env-only": (
        {"tox.ini": "[testenv:py]\ncommands = python -m unittest discover\n"},
        "tox",
    ),
    "tox-unrelated-section": ({"tox.ini": "[testenvironment]\nx = 1\n"}, None),
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


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"Cargo.toml": "[package]\nname = 'a'\n"}, "cargo test"),
        ({"Makefile": "test:\n\ttrue\n"}, "make test"),
        ({"GNUmakefile": "test::\n\ttrue\n"}, "make test"),
        ({}, None),
    ],
)
def test_detect_runner_without_python(tmp_path, files, expected):
    # Only grep is available: invoke Bash by absolute path, and expose no Python.
    bash = shutil.which("bash")
    grep = shutil.which("grep")
    assert bash is not None and grep is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "grep").symlink_to(grep)
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    result = subprocess.run(
        [bash, str(SCRIPT), str(tmp_path)],
        env={**os.environ, "PATH": str(bin_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if expected else 1), result.stderr
    assert result.stdout.split("  #")[0].strip() == (expected or "")


@pytest.mark.parametrize(
    "preferred,shadowed",
    [
        ("GNUmakefile", "makefile"),
        ("GNUmakefile", "Makefile"),
        ("makefile", "Makefile"),
    ],
)
def test_make_uses_first_existing_default_file(tmp_path, preferred, shadowed):
    (tmp_path / preferred).write_text("all:\n\ttrue\n")
    (tmp_path / shadowed).write_text("test:\n\ttrue\n")
    make = subprocess.run(
        ["make", "--no-print-directory", "-n", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert make.returncode != 0  # The shadowed test target is not executable.
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 1, result.stdout
    assert result.stdout == ""


def test_npm_lifecycle_scripts_shown(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"pretest": "echo pre", "test": "jest"}}'
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "npm lifecycle pretest: echo pre" in result.stderr
