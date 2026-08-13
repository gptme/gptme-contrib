#!/usr/bin/env python3
"""Check that package install sizes don't exceed configured budgets.

Each package can set a max_install_mb in pyproject.toml [tool.gptme-contrib]:

    [tool.gptme-contrib]
    max_install_mb = 500  # Fail if install size exceeds this

This prevents silent regressions like the torch CUDA bloat (5.2GB) discovered
in gptme-contrib#1416.
"""

import argparse
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_BUDGETS = {
    "pure-python": 100,  # MB, for packages with no compiled deps
    "ml-optional": 2000,  # MB, for packages with optional ML deps
    "other": 500,  # MB, catch-all default
}

# Keywords in dependency lists that indicate an ML package (ml-optional budget)
_ML_DEP_KEYWORDS = ("torch", "tensorflow", "jax", "onnx", "transformers")


def _infer_package_category(config: dict) -> str:
    """Infer the budget category from pyproject.toml dependency lists.

    Returns one of: 'pure-python', 'ml-optional', 'other'.
    """
    deps: list[str] = config.get("project", {}).get("dependencies", [])
    optional_groups = config.get("project", {}).get("optional-dependencies", {})
    all_deps = deps + [d for group in optional_groups.values() for d in group]

    lowered = " ".join(all_deps).lower()
    if any(kw in lowered for kw in _ML_DEP_KEYWORDS):
        return "ml-optional"

    # No compiled deps heuristic: packages with no dependencies or only
    # pure-Python ones (no binary wheels) get the tighter 100MB limit.
    # We treat "no dependencies" as pure-Python to catch accidental bloat early.
    if not all_deps:
        return "pure-python"

    return "other"


def get_package_budget(pyproject_path: Path) -> int:
    """Extract max_install_mb from package pyproject.toml.

    Falls back to a category-inferred default when max_install_mb is not set:
      - packages with ML deps (torch, tensorflow, …) → 2000MB
      - packages with no declared dependencies → 100MB (pure-Python)
      - everything else → 500MB
    """
    if not pyproject_path.exists():
        return DEFAULT_BUDGETS["other"]

    with open(pyproject_path, "rb") as f:
        config = tomllib.load(f)
    budget = config.get("tool", {}).get("gptme-contrib", {}).get("max_install_mb")
    if budget is not None:
        return int(budget)
    category = _infer_package_category(config)
    return DEFAULT_BUDGETS[category]


def measure_install_size(package_path: Path, package_name: str) -> float | None:
    """Install package in a temp venv and measure disk usage.

    First tries to export workspace-locked requirements via ``uv export --package``.
    This reads ``uv.lock`` and respects workspace-level ``[tool.uv.sources]`` and
    ``[tool.uv.index]`` settings (e.g. a CPU-only torch index), making the
    measurement consistent with what ``uv sync`` would actually install.

    Falls back to a direct ``uv pip install`` when the package is not yet in the
    workspace lock (e.g. a brand-new package being added). That fallback resolves
    from PyPI and may overestimate size for index-pinned packages.

    Returns:
        Size in MB, or None if installation failed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        venv_path = tmpdir / "venv"

        try:
            # Create venv with uv
            subprocess.run(
                ["uv", "venv", str(venv_path)],
                check=True,
                capture_output=True,
                timeout=60,
            )

            python_exe = venv_path / "bin" / "python"
            if not python_exe.exists():
                print(f"❌ Failed to create venv for {package_name}")
                return None

            # Try to export workspace-locked requirements. uv export reads uv.lock,
            # which includes [tool.uv.index] overrides (e.g. CPU-only torch index).
            # --no-hashes produces a plain requirements.txt; --emit-index-url
            # injects the index URLs so uv pip resolves from the correct source.
            export_result = subprocess.run(
                [
                    "uv",
                    "export",
                    "--format",
                    "requirements-txt",
                    "--package",
                    package_name,
                    "--no-dev",
                    "--no-hashes",
                    "--emit-index-url",
                ],
                cwd=package_path.parent.parent,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if export_result.returncode == 0 and export_result.stdout.strip():
                # Install from workspace-locked requirements
                req_file = tmpdir / "requirements.txt"
                req_file.write_text(export_result.stdout)
                install_cmd = [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_exe),
                    "--quiet",
                    "-r",
                    str(req_file),
                    str(package_path),
                ]
            else:
                # Package not in workspace lock; fall back to direct PyPI install.
                if export_result.returncode != 0:
                    print(
                        f"⚠ Lock export failed for {package_name}, using PyPI fallback"
                    )
                install_cmd = [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_exe),
                    "--index-strategy",
                    "unsafe-best-match",
                    "--quiet",
                    str(package_path),
                ]

            result = subprocess.run(
                install_cmd,
                cwd=package_path.parent.parent,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                print(f"❌ Installation failed for {package_name}")
                print(result.stderr)
                return None

            # Measure venv size. lstat, not is_file(follow_symlinks=False):
            # that kwarg is 3.13+, and every venv has symlinks, so on 3.12 it
            # raised TypeError and the whole measurement returned None.
            total_size = 0
            for entry in venv_path.rglob("*"):
                try:
                    entry_stat = entry.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(entry_stat.st_mode):
                    total_size += entry_stat.st_size

            size_mb = total_size / (1024 * 1024)
            return size_mb

        except subprocess.TimeoutExpired:
            print(f"❌ Installation timed out for {package_name}")
            return None
        except Exception as e:
            print(f"❌ Error checking {package_name}: {e}")
            return None


def check_packages(
    packages_dir: Path, verbose: bool = False, only: str | None = None
) -> bool:
    """Check all packages in packages/ directory.

    Args:
        only: If set, check just this one package (measuring every package
            builds a venv each, so local runs want a single target).

    Returns:
        True if all packages pass, False if any exceed their budget.
    """
    all_pass = True
    packages_dir = packages_dir.resolve()

    package_paths = sorted(
        [p for p in packages_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    )
    if only:
        package_paths = [p for p in package_paths if p.name == only]
        if not package_paths:
            print(f"❌ No package named {only!r} in {packages_dir}")
            return False

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
            print(f"✗ {package_path.name:40} installation failed")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--package", help="Check only this package (default: all packages)"
    )
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    packages_dir = repo_root / "packages"

    if shutil.which("uv") is None:
        print("❌ uv not found on PATH — required to resolve workspace sources")
        sys.exit(1)

    print("Checking package install sizes...")
    print()

    if check_packages(packages_dir, args.verbose, args.package):
        print()
        print("✓ All packages within budget")
        sys.exit(0)
    else:
        print()
        print("✗ One or more packages exceeded budget")
        sys.exit(1)
