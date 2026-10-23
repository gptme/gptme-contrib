"""Guard: every package with a tests/ directory must be in the CI package matrix.

`make ci-list-packages-json` used to list a package only when it had a
Makefile, so a package with tests but no Makefile got no CI job at all. A
stale test expectation in gptme-usage survived a dependency pin bump for 11
days because of exactly that (gptme/gptme-contrib#1973).

This test fails if a package with a tests/ directory is missing from the
matrix, or if a package without tests/ is in it.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"


def _matrix_packages(repo_root: Path = REPO_ROOT) -> list[str]:
    result = subprocess.run(
        ["make", "--no-print-directory", "ci-list-packages-json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    # make can emit "Entering directory" chatter on stdout, so take the last
    # line that parses as a JSON array rather than assuming the whole output.
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        packages: list[str] = json.loads(line)
        return packages
    raise AssertionError(f"no JSON array in make output: {result.stdout!r}")


def _packages_with_tests() -> set[str]:
    return {
        path.name
        for path in PACKAGES_DIR.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and path.name != "__pycache__"
        and (path / "tests").is_dir()
    }


def test_every_package_with_tests_is_in_the_ci_matrix():
    """A package with tests/ but no CI job is a silent coverage loss."""
    in_matrix = set(_matrix_packages())
    missing = _packages_with_tests() - in_matrix
    assert not missing, (
        "packages with tests/ but no CI matrix entry: "
        f"{sorted(missing)} — add a Makefile or fix the discovery rule"
    )


def test_matrix_contains_no_packages_without_tests():
    """The matrix must not list packages that have no test suite to run."""
    without_tests = set(_matrix_packages()) - _packages_with_tests()
    assert not without_tests, f"matrix entries without tests/: {sorted(without_tests)}"


def test_matrix_ignores_symlinked_packages_and_cache(tmp_path: Path):
    """Only real package directories belong in the matrix, not aliases or caches."""
    (tmp_path / "Makefile").write_text((REPO_ROOT / "Makefile").read_text())
    packages = tmp_path / "packages"
    (packages / "real-package" / "tests").mkdir(parents=True)
    (packages / "package-link").symlink_to("real-package", target_is_directory=True)
    (packages / "__pycache__" / "tests").mkdir(parents=True)
    (packages / "no-tests").mkdir()

    assert _matrix_packages(tmp_path) == ["real-package"]
