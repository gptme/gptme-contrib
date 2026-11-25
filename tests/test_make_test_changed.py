"""`make test-changed-packages` must test changed plugins, not only packages (#1824)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("git") is None,
    reason="needs make and git",
)


def _git_env() -> dict[str, str]:
    # Hooks export repository selectors; never let them redirect fixture writes.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=_git_env(),
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


def _run_changed(repo: Path, *paths: str, make_command: str = "echo") -> str:
    for p in paths:
        (repo / p).write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    # MAKE=echo prints the recursive invocations instead of running real tests.
    out = subprocess.run(
        ["make", "-s", "test-changed-packages", "BASE=HEAD~1", f"MAKE={make_command}"],
        cwd=repo,
        env=_git_env(),
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


@pytest.mark.parametrize("path", ["Makefile", "pyproject.toml", "uv.lock", "README.md"])
def test_fallback_tests_packages_and_plugins(repo: Path, path: str) -> None:
    # Keep the real Makefile executable when marking shared config as changed.
    if path == "Makefile":
        with (repo / path).open("a") as f:
            f.write("\n# changed\n")
        out = _run_changed(repo)
    else:
        out = _run_changed(repo, path)
    assert "test-packages" in out
    assert "test-plugins" in out


@pytest.mark.parametrize("shared_config", [False, True])
def test_fallback_runs_plugins_even_when_packages_fail(
    repo: Path, shared_config: bool
) -> None:
    runner = repo / "run-tests.sh"
    runner.write_text(
        "#!/bin/sh\n" 'echo "$1" >> invocations.log\n' '[ "$1" != test-packages ]\n'
    )
    runner.chmod(0o755)
    paths = ("pyproject.toml",) if shared_config else ("README.md",)
    with pytest.raises(subprocess.CalledProcessError):
        _run_changed(repo, *paths, make_command="./run-tests.sh")
    assert (repo / "invocations.log").read_text().splitlines() == [
        "test-packages",
        "test-plugins",
    ]


@pytest.mark.parametrize("repository_selectors", [False, True])
def test_git_environment_is_isolated(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_selectors: bool,
) -> None:
    config = tmp_path / "host.gitconfig"
    config.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    if repository_selectors:
        # Invalid paths fail safely on the old helper, without touching a real index.
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repository"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "not-a-worktree"))
        monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "not-an-index"))
    out = _run_changed(repo, "plugins/plug-a/tests/test_x.py")
    assert "test-plugin PLUGIN=plug-a" in out
