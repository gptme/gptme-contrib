"""Tests for the install size checker script."""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts directory to path for imports
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_default_budgets():
    """Test that default budgets are defined."""
    import check_install_size

    assert check_install_size.DEFAULT_BUDGETS["pure-python"] == 100
    assert check_install_size.DEFAULT_BUDGETS["ml-optional"] == 2000
    assert check_install_size.DEFAULT_BUDGETS["other"] == 500


def test_get_package_budget_missing_file():
    """Test that missing pyproject.toml returns default budget."""
    import check_install_size

    nonexistent = Path("/nonexistent/pyproject.toml")
    budget = check_install_size.get_package_budget(nonexistent)
    assert budget == check_install_size.DEFAULT_BUDGETS["other"]


def test_get_package_budget_from_pyproject():
    """Test reading budget from pyproject.toml."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text(
            """
[tool.gptme-contrib]
max_install_mb = 750
"""
        )

        budget = check_install_size.get_package_budget(pyproject_path)
        assert budget == 750


def test_get_package_budget_missing_tool_section():
    """Test that pyproject without [tool.gptme-contrib] uses inferred default."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text(
            """
[project]
name = "test"
"""
        )

        # No dependencies → inferred as pure-python
        budget = check_install_size.get_package_budget(pyproject_path)
        assert budget == check_install_size.DEFAULT_BUDGETS["pure-python"]


def test_get_package_budget_infers_ml_category():
    """Test that torch in deps triggers the ml-optional budget."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text(
            """
[project]
name = "ml-pkg"
dependencies = ["torch>=2.0"]
"""
        )

        budget = check_install_size.get_package_budget(pyproject_path)
        assert budget == check_install_size.DEFAULT_BUDGETS["ml-optional"]


def test_get_package_budget_infers_other_category():
    """Test that non-ML dependencies fall back to the 'other' budget."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text(
            """
[project]
name = "some-pkg"
dependencies = ["requests>=2.0", "click>=8.0"]
"""
        )

        budget = check_install_size.get_package_budget(pyproject_path)
        assert budget == check_install_size.DEFAULT_BUDGETS["other"]


def _make_fake_venv(venv_path: Path, file_size_bytes: int) -> None:
    """Write a single regular file into a fake venv directory."""
    venv_path.mkdir(parents=True)
    fake_lib = venv_path / "lib" / "fake_pkg.py"
    fake_lib.parent.mkdir(parents=True)
    fake_lib.write_bytes(b"x" * file_size_bytes)


def test_measure_install_size_success():
    """measure_install_size returns MB when install succeeds (lock-export path)."""
    import check_install_size

    file_size = 10 * 1024 * 1024  # 10 MB

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "venv" in cmd:
            # Materialise a fake venv at the path uv would create
            venv_path = Path(cmd[-1])
            _make_fake_venv(venv_path, file_size)
            (venv_path / "bin").mkdir(exist_ok=True)
            (venv_path / "bin" / "python").touch()
            result.stdout = ""
        elif "export" in cmd:
            # Simulate successful lock export; content triggers the lock-export path
            result.stdout = "pkg==1.0.0\n"
        elif "du" in cmd:
            # du -sAb returns "{bytes}\t{path}"
            result.stdout = f"{file_size}\t{cmd[-1]}\n"
        else:
            result.stdout = ""
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert size is not None
    assert 9.0 < size < 11.0  # ~10 MB


def test_measure_install_size_export_fallback():
    """measure_install_size falls back to direct install when lock export fails."""
    import check_install_size

    file_size = 5 * 1024 * 1024  # 5 MB

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stderr = ""
        if "venv" in cmd:
            result.returncode = 0
            venv_path = Path(cmd[-1])
            _make_fake_venv(venv_path, file_size)
            (venv_path / "bin").mkdir(exist_ok=True)
            (venv_path / "bin" / "python").touch()
            result.stdout = ""
        elif "export" in cmd:
            # Simulate lock export failure (package not in lock yet)
            result.returncode = 1
            result.stdout = ""
        elif "du" in cmd:
            result.returncode = 0
            result.stdout = f"{file_size}\t{cmd[-1]}\n"
        else:
            # Direct pip install falls back and succeeds
            result.returncode = 0
            result.stdout = ""
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert size is not None
    assert 4.0 < size < 6.0  # ~5 MB


def test_measure_install_size_install_failure():
    """measure_install_size returns None when uv pip install fails."""
    import check_install_size

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if "venv" in cmd:
            result.returncode = 0
            venv_path = Path(cmd[-1])
            venv_path.mkdir(parents=True)
            (venv_path / "bin").mkdir()
            (venv_path / "bin" / "python").touch()
        else:
            result.returncode = 1
            result.stderr = "ERROR: package not found"
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert size is None


def test_measure_install_size_timeout():
    """measure_install_size returns None on timeout."""
    import check_install_size

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert size is None


def test_check_packages_pass_and_fail():
    """check_packages returns True for passing packages and False when over budget."""
    import check_install_size

    # Stub measure_install_size to return fixed sizes
    sizes = {"passing": 50.0, "failing": 600.0}

    def fake_measure(package_path, package_name):
        return sizes.get(package_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"

        for name in ("passing", "failing"):
            pkg_dir = packages_dir / name
            pkg_dir.mkdir(parents=True)
            # Budget: 100MB for both; failing will exceed it
            (pkg_dir / "pyproject.toml").write_text(
                "[tool.gptme-contrib]\nmax_install_mb = 100\n"
            )

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result_pass = check_install_size.check_packages(
                packages_dir, verbose=False, only="passing"
            )
            result_fail = check_install_size.check_packages(
                packages_dir, verbose=False, only="failing"
            )

    assert result_pass is True
    assert result_fail is False


def test_check_packages_none_size_fails():
    """check_packages returns False when measure_install_size returns None."""
    import check_install_size

    def fake_measure(package_path, package_name):
        return None  # simulates install failure

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"
        pkg_dir = packages_dir / "broken"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(
                packages_dir, verbose=False, only="broken"
            )

    assert result is False


def test_check_packages_skips_symlinks():
    """check_packages silently skips symlinked package directories."""
    import check_install_size

    call_count = {"n": 0}

    def fake_measure(package_path, package_name):
        call_count["n"] += 1
        return 10.0

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"
        packages_dir.mkdir()

        # Real package
        real_pkg = packages_dir / "real"
        real_pkg.mkdir()
        (real_pkg / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        # Symlinked package — should be skipped, not measured
        target = Path(tmpdir) / "external"
        target.mkdir()
        (packages_dir / "linked").symlink_to(target)

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(packages_dir, verbose=True)

    assert result is True
    assert call_count["n"] == 1, "symlinked package should not be measured"


def test_check_packages_bad_budget_does_not_crash():
    """check_packages handles malformed max_install_mb without crashing the whole run."""
    import check_install_size

    call_count = {"n": 0}

    def fake_measure(package_path, package_name):
        call_count["n"] += 1
        return 10.0

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"

        # Good package
        good = packages_dir / "good_pkg"
        good.mkdir(parents=True)
        (good / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        # Malformed budget (string instead of int)
        bad = packages_dir / "bad_budget"
        bad.mkdir(parents=True)
        (bad / "pyproject.toml").write_text(
            '[tool.gptme-contrib]\nmax_install_mb = "large"\n'
        )

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(packages_dir, verbose=False)

    # Script must not crash; result is False (one package failed) but good_pkg was measured
    assert result is False
    assert call_count["n"] == 1, "good package should still be measured"


def test_check_packages_skips_no_pyproject():
    """check_packages skips packages without a pyproject.toml."""
    import check_install_size

    call_count = {"n": 0}

    def fake_measure(package_path, package_name):
        call_count["n"] += 1
        return 10.0

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"
        packages_dir.mkdir()

        # Package with pyproject.toml — should be measured
        with_toml = packages_dir / "has_toml"
        with_toml.mkdir()
        (with_toml / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        # Package directory without pyproject.toml — should be skipped
        no_toml = packages_dir / "no_toml"
        no_toml.mkdir()

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(packages_dir, verbose=True)

    assert result is True
    assert call_count["n"] == 1, "package without pyproject.toml should not be measured"
