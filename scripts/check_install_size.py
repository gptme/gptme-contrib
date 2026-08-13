#!/usr/bin/env python3
"""Check that package install sizes don't exceed configured budgets.

Each package can set a max_install_mb in pyproject.toml [tool.gptme-contrib]:

    [tool.gptme-contrib]
    max_install_mb = 500  # Fail if install size exceeds this

This prevents silent regressions like the torch CUDA bloat (5.2GB) discovered
in gptme-contrib#1416.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib


DEFAULT_BUDGETS = {
    "pure-python": 100,  # MB, for packages with no compiled deps
    "ml-optional": 2000,  # MB, for packages with optional ML deps
    "other": 500,  # MB, catch-all default
}


def get_package_budget(pyproject_path: Path) -> int:
    """Extract max_install_mb from package pyproject.toml."""
    if not pyproject_path.exists():
        return DEFAULT_BUDGETS["other"]

    try:
        with open(pyproject_path, "rb") as f:
            config = tomllib.load(f)
        budget = config.get("tool", {}).get("gptme-contrib", {}).get("max_install_mb")
        return budget if budget is not None else DEFAULT_BUDGETS["other"]
    except Exception:
        return DEFAULT_BUDGETS["other"]


def measure_install_size(package_path: Path, package_name: str) -> Optional[int]:
    """Install package in a temp venv and measure disk usage.

    Returns:
        Size in MB, or None if installation failed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        venv_path = tmpdir / "venv"

        try:
            # Create venv
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                check=True,
                capture_output=True,
            )

            # Get pip executable from venv
            pip_exe = venv_path / "bin" / "pip"
            if not pip_exe.exists():
                print(f"❌ Failed to create venv for {package_name}")
                return None

            # Install package
            result = subprocess.run(
                [
                    str(pip_exe),
                    "install",
                    "--quiet",
                    str(package_path),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                print(f"❌ Installation failed for {package_name}")
                print(result.stderr)
                return None

            # Measure venv size
            total_size = 0
            for entry in venv_path.rglob("*"):
                try:
                    if entry.is_file(follow_symlinks=False):
                        total_size += entry.stat().st_size
                except OSError:
                    pass

            size_mb = total_size / (1024 * 1024)
            return size_mb

        except Exception as e:
            print(f"❌ Error checking {package_name}: {e}")
            return None


def check_packages(packages_dir: Path, verbose: bool = False) -> bool:
    """Check all packages in packages/ directory.

    Returns:
        True if all packages pass, False if any exceed their budget.
    """
    all_pass = True
    packages_dir = packages_dir.resolve()

    package_paths = sorted(
        [p for p in packages_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    )

    for package_path in package_paths:
        # Skip symlinks (they point to gptme-contrib submodule)
        if package_path.is_symlink():
            if verbose:
                print(f"⊘ {package_path.name:40} (symlink, skipped)")
            continue

        pyproject_path = package_path / "pyproject.toml"
        if not pyproject_path.exists():
            if verbose:
                print(f"⊘ {package_path.name:40} (no pyproject.toml)")
            continue

        budget_mb = get_package_budget(pyproject_path)
        size_mb = measure_install_size(package_path, package_path.name)

        if size_mb is None:
            all_pass = False
            continue

        status = "✓" if size_mb <= budget_mb else "✗"
        pct = (size_mb / budget_mb * 100) if budget_mb > 0 else 0
        print(
            f"{status} {package_path.name:40} {size_mb:8.1f}MB / {budget_mb:5}MB ({pct:5.1f}%)"
        )

        if size_mb > budget_mb:
            all_pass = False

    return all_pass


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    packages_dir = repo_root / "packages"

    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    print("Checking package install sizes...")
    print()

    if check_packages(packages_dir, verbose):
        print()
        print("✓ All packages within budget")
        sys.exit(0)
    else:
        print()
        print("✗ One or more packages exceeded budget")
        sys.exit(1)
