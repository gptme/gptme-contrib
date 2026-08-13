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
    seen_commands = []

    def fake_run(cmd, **kwargs):
        seen_commands.append(cmd)
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
    export_cmd = next(cmd for cmd in seen_commands if "export" in cmd)
    install_cmd = next(cmd for cmd in seen_commands if "install" in cmd)
    assert "--emit-index-url" not in export_cmd
    assert export_cmd[-2:] == ["--index-strategy", "unsafe-best-match"]
    assert "--index-strategy" in install_cmd
    assert "unsafe-best-match" in install_cmd


def test_measure_install_size_export_fallback():
    """measure_install_size falls back to direct install when lock export fails."""
    import check_install_size

    file_size = 5 * 1024 * 1024  # 5 MB
    seen_commands = []

    def fake_run(cmd, **kwargs):
        seen_commands.append(cmd)
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
    install_cmd = next(cmd for cmd in seen_commands if "install" in cmd)
    assert "--index-strategy" in install_cmd
    assert "unsafe-best-match" in install_cmd


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
    """check_packages reports budget overages separately from clean passes."""
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

    assert result_pass == (True, 0, 0)
    assert result_fail == (False, 0, 1)


def test_check_packages_none_size_fails():
    """check_packages counts install failures before size measurement."""
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

    assert result == (False, 1, 0)


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

    assert result == (True, 0, 0)
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

    # Script must not crash; one package failed before size measurement and good_pkg
    # was still measured.
    assert result == (False, 1, 0)
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

    assert result == (True, 0, 0)
    assert call_count["n"] == 1, "package without pyproject.toml should not be measured"


def test_print_failure_summary_lists_only_present_classes(capsys):
    """Failure summary should name config errors, install failures, and budget overages separately."""
    import check_install_size

    result = check_install_size.CheckResult(
        all_pass=False, config_errors=2, install_failures=1, budget_overages=1
    )
    check_install_size.print_failure_summary(result)

    out = capsys.readouterr().out
    assert "2 packages had invalid budget configuration" in out
    assert "1 package failed to install" in out
    assert "1 package exceeded their install-size budget" in out


def test_print_failure_summary_omits_zero_count_classes(capsys):
    """Zero-count failure classes should not print a misleading summary line."""
    import check_install_size

    result = check_install_size.CheckResult(
        all_pass=False, config_errors=0, install_failures=0, budget_overages=1
    )
    check_install_size.print_failure_summary(result)

    out = capsys.readouterr().out
    assert "invalid budget configuration" not in out
    assert "failed to install" not in out
    assert "1 package exceeded their install-size budget" in out


def test_check_result_bool_semantics():
    """CheckResult.__bool__ returns all_pass so 'if not result' works for failed checks."""
    import check_install_size

    passing = check_install_size.CheckResult(True, 0, 0, 0)
    failing = check_install_size.CheckResult(False, 1, 0, 0)

    assert bool(passing) is True
    assert bool(failing) is False
    # Crucially: a failing tuple is always truthy; CheckResult is not
    assert not failing


def test_check_result_distinguishes_config_errors_from_install_failures(capsys):
    """Config errors and install failures should produce distinct summary lines."""
    import check_install_size

    result = check_install_size.CheckResult(
        all_pass=False, config_errors=1, install_failures=2, budget_overages=0
    )
    check_install_size.print_failure_summary(result)

    out = capsys.readouterr().out
    # Config error → directs to pyproject.toml, not install command
    assert "invalid budget configuration" in out
    # Install failure → directs to install command
    assert "2 packages failed to install" in out
    # No budget overage line
    assert "exceeded their install-size budget" not in out


def test_check_result_tuple_unpacking_backward_compat():
    """Tuple-unpacking as (all_pass, pre_measurement_failures, budget_overages) still works."""
    import check_install_size

    result = check_install_size.CheckResult(
        all_pass=False, config_errors=1, install_failures=2, budget_overages=3
    )
    all_pass, pre_measurement_failures, budget_overages = result
    assert all_pass is False
    assert pre_measurement_failures == 3  # config_errors + install_failures
    assert budget_overages == 3
