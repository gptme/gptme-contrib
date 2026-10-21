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


def _matrix_packages() -> list[str]:
    result = subprocess.run(
        ["make", "ci-list-packages-json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    packages: list[str] = json.loads(result.stdout)
    return packages


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
