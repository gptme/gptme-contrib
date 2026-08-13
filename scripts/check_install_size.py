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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_BUDGETS = {
    "pure-python": 100,  # MB, for packages with no compiled deps
    "ml-optional": 2000,  # MB, for packages with optional ML deps
    "other": 500,  # MB, catch-all default
}


class CheckResult:
    """Result of check_packages.

    Evaluates as ``bool`` for backward-compatible ``if not check_packages(...)``
    checks (returns ``all_pass``).  Also supports tuple-unpacking as
    ``(all_pass, pre_measurement_failures, budget_overages)`` so callers that
    already unpack the tuple continue to work unchanged.

    Prefer attribute access for new code:
    - ``result.all_pass`` — overall pass/fail
    - ``result.config_errors`` — packages with invalid budget config
    - ``result.install_failures`` — packages that failed to install
    - ``result.budget_overages`` — packages that exceeded their size budget
    - ``result.pre_measurement_failures`` — config_errors + install_failures (combined)
    """

    def __init__(
        self,
        all_pass: bool,
        config_errors: int,
        install_failures: int,
        budget_overages: int,
    ) -> None:
        self.all_pass = all_pass
        self.config_errors = config_errors
        self.install_failures = install_failures
        self.budget_overages = budget_overages

    @property
    def pre_measurement_failures(self) -> int:
        return self.config_errors + self.install_failures

    def __bool__(self) -> bool:
        return self.all_pass

    def __iter__(self) -> Iterator:
        """Yield (all_pass, pre_measurement_failures, budget_overages) for tuple unpacking."""
        yield self.all_pass
        yield self.pre_measurement_failures
        yield self.budget_overages

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return tuple(self) == other
        if isinstance(other, CheckResult):
            return (
                self.all_pass == other.all_pass
                and self.config_errors == other.config_errors
                and self.install_failures == other.install_failures
                and self.budget_overages == other.budget_overages
            )
        return NotImplemented

    def __repr__(self) -> str:
        return (
            f"CheckResult(all_pass={self.all_pass!r}, "
            f"config_errors={self.config_errors!r}, "
            f"install_failures={self.install_failures!r}, "
            f"budget_overages={self.budget_overages!r})"
        )


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
            # unsafe-best-match is required because PyTorch's CPU index mirrors
            # some non-torch packages with older versions; first-index would make
            # normal PyPI releases invisible once that mirror has any match.
            #
            # Note: we do NOT pass --emit-index-url here. Index URLs are not needed
            # in the generated requirements.txt because the subsequent uv pip install
            # runs with cwd=package_path.parent.parent (the workspace root), so uv
            # reads [tool.uv.index] from the workspace pyproject.toml directly and
            # applies the same pytorch-cpu index override without being told via
            # requirements-file headers. CI confirms: gptme-rag installs at ~1200MB
            # (CPU torch), not ~5GB (CUDA build from plain PyPI).
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
                    "--index-strategy",
                    "unsafe-best-match",
                ],
                cwd=package_path.parent.parent,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if export_result.returncode == 0 and export_result.stdout.strip():
                # Filter out git+ URL lines before installing. The lock pins workspace
                # git deps (e.g. gptme) to a specific commit SHA, but installing the
                # package source alongside that requirements file causes a conflict:
                # the package's pyproject.toml (read via workspace sources) references
                # the same dep at @master. Dropping git+ lines lets uv resolve those
                # deps from the package's own declarations + workspace sources.
                req_lines = [
                    line
                    for line in export_result.stdout.splitlines()
                    if " @ git+" not in line and not line.strip().startswith("git+")
                ]
                req_file = tmpdir / "requirements.txt"
                req_file.write_text("\n".join(req_lines) + "\n")
                install_cmd = [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_exe),
                    "--index-strategy",
                    "unsafe-best-match",
                    "--quiet",
                    "-r",
                    str(req_file),
                    str(package_path),
                ]
            else:
                # Package not in workspace lock; fall back to direct PyPI install.
                # This path ignores [tool.uv.index] and [tool.uv.sources] workspace
                # overrides, so measurements may overestimate for packages with custom
                # index pins (e.g. CPU-only torch). Run `uv lock` after adding the
                # package to get accurate measurements from the workspace lock.
                if export_result.returncode != 0:
                    print(
                        f"⚠ Lock export failed for {package_name}; using PyPI fallback "
                        f"(may overestimate — add to uv.lock for accurate measurement)"
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

            # cwd=workspace root so uv reads [tool.uv.index] (pytorch-cpu) from
            # pyproject.toml, making --emit-index-url on the export unnecessary.
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

            # Measure venv disk usage with `du -sb` (bytes). du counts each
            # hard-linked inode once, which matters when uv hard-links cached
            # wheels into venvs. Note: -b uses lstat() for symlinks, reporting
            # the path length (a few bytes) rather than the target file size.
            # In practice this is fine: pip packages don't install large
            # external files as symlinks, and the Python interpreter symlink
            # (bin/python) is intentionally not counted since the interpreter
            # is not part of the package's dependency footprint.
            du_result = subprocess.run(
                ["du", "-sb", str(venv_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            size_mb = int(du_result.stdout.split()[0]) / (1024 * 1024)
            return size_mb

        except subprocess.TimeoutExpired:
            print(f"❌ Installation timed out for {package_name}")
            return None
        except Exception as e:
            print(f"❌ Error checking {package_name}: {e}")
            return None


def print_failure_summary(result: CheckResult) -> None:
    """Print only the failure classes that actually occurred.

    Uses precise labels for each class so the summary directs developers to the
    right place:
    - config errors → check pyproject.toml
    - install failures → check the install command / environment
    - budget overages → trim dependencies or raise the budget
    """
    if result.config_errors > 0:
        plural = "package" if result.config_errors == 1 else "packages"
        print(
            f"✗ {result.config_errors} {plural} had invalid budget configuration in pyproject.toml "
            f"(see per-package output above)"
        )
    if result.install_failures > 0:
        plural = "package" if result.install_failures == 1 else "packages"
        print(
            f"✗ {result.install_failures} {plural} failed to install "
            f"(see per-package output above)"
        )
    if result.budget_overages > 0:
        plural = "package" if result.budget_overages == 1 else "packages"
        print(f"✗ {result.budget_overages} {plural} exceeded their install-size budget")


def check_packages(
    packages_dir: Path, verbose: bool = False, only: str | None = None
) -> CheckResult:
    """Check all packages in packages/ directory.

    Args:
        only: If set, check just this one package (measuring every package
            builds a venv each, so local runs want a single target).

    Returns:
        :class:`CheckResult` with counts for each failure class.
        Evaluates as ``bool`` for backward-compatible ``if not check_packages(...)`` use.
    """
    all_pass = True
    config_errors = 0
    install_failures = 0
    budget_overages = 0
    packages_dir = packages_dir.resolve()

    package_paths = sorted(
        [p for p in packages_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    )
    if only:
        package_paths = [p for p in package_paths if p.name == only]
        if not package_paths:
            print(f"❌ No package named {only!r} in {packages_dir}")
            return CheckResult(False, 0, 0, 0)

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

        try:
            budget_mb = get_package_budget(pyproject_path)
        except (ValueError, TypeError) as e:
            print(f"✗ {package_path.name:40} bad max_install_mb in pyproject.toml: {e}")
            all_pass = False
            config_errors += 1
            continue

        # Read the canonical distribution name from pyproject.toml [project] name.
        # Directory names and distribution names can diverge (e.g. hyphens vs
        # underscores), and `uv export --package` requires the registered name.
        try:
            with open(pyproject_path, "rb") as _f:
                _pkg_meta = tomllib.load(_f)
            package_name = _pkg_meta.get("project", {}).get("name") or package_path.name
        except Exception:
            package_name = package_path.name

        size_mb = measure_install_size(package_path, package_name)

        if size_mb is None:
            print(f"✗ {package_path.name:40} installation failed")
            all_pass = False
            install_failures += 1
            continue

        status = "✓" if size_mb <= budget_mb else "✗"
        pct = (size_mb / budget_mb * 100) if budget_mb > 0 else 0
        print(
            f"{status} {package_path.name:40} {size_mb:8.1f}MB / {budget_mb:5}MB ({pct:5.1f}%)"
        )

        if size_mb > budget_mb:
            all_pass = False
            budget_overages += 1

    return CheckResult(all_pass, config_errors, install_failures, budget_overages)


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

    result = check_packages(packages_dir, args.verbose, args.package)

    if result:
        print()
        print("✓ All packages within budget")
        sys.exit(0)
    else:
        print()
        print_failure_summary(result)
        sys.exit(1)
