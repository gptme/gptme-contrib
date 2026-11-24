"""`make test-changed-packages` must test changed plugins, not only packages (#1824)."""

import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("git") is None,
    reason="needs make and git",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    shutil.copy(MAKEFILE, tmp_path / "Makefile")
    for d in ("packages/pkg-a/tests", "plugins/plug-a/tests"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "test_x.py").write_text("")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _run_changed(repo: Path, *paths: str) -> str:
    for p in paths:
        (repo / p).write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    # MAKE=echo prints the recursive invocations instead of running real tests.
    out = subprocess.run(
        ["make", "-s", "test-changed-packages", "BASE=HEAD~1", "MAKE=echo"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def test_plugin_only_change_tests_the_plugin(repo: Path) -> None:
    out = _run_changed(repo, "plugins/plug-a/tests/test_x.py")
    assert "test-plugin PLUGIN=plug-a" in out
    assert "test-packages" not in out


def test_package_and_plugin_change_tests_both(repo: Path) -> None:
    out = _run_changed(
        repo, "packages/pkg-a/tests/test_x.py", "plugins/plug-a/tests/test_x.py"
    )
    assert "test-package PKG=pkg-a" in out
    assert "test-plugin PLUGIN=plug-a" in out
    assert "test-packages" not in out


def test_package_only_change_unchanged(repo: Path) -> None:
    out = _run_changed(repo, "packages/pkg-a/tests/test_x.py")
    assert "test-package PKG=pkg-a" in out
    assert "test-plugin" not in out
