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
            result.stdout = (
                "pkg==1.0.0\n"
                "gptme @ git+https://github.com/gptme/gptme.git@deadbeef\n"
                "anthropic==0.120.2\n"
            )
        elif "du" in cmd:
            # du -sAb returns "{bytes}\t{path}"
            result.stdout = f"{file_size}\t{cmd[-1]}\n"
        else:
            if "install" in cmd:
                requirements = Path(cmd[cmd.index("-r") + 1]).read_text()
                assert (
                    "gptme @ git+https://github.com/gptme/gptme.git@deadbeef"
                    in requirements
                )
                assert "anthropic==0.120.2" in requirements
            result.stdout = ""
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert isinstance(size, float), f"expected float, got {size!r}"
    assert 9.0 < size < 11.0  # ~10 MB
    export_cmd = next(cmd for cmd in seen_commands if "export" in cmd)
    install_cmd = next(cmd for cmd in seen_commands if "install" in cmd)
    assert "--no-sources" in install_cmd
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

    assert isinstance(size, float), f"expected float, got {size!r}"
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


def test_check_packages_mixed_failures(capsys):
    """check_packages counts config errors and install failures independently.

    A malformed budget config must not spill into install_failures and vice versa.
    Both counters must accumulate separately so print_failure_summary can direct
    developers to the right place (pyproject.toml vs the install command).
    """
    import check_install_size

    def fake_measure(package_path, package_name):
        # Only called for the package with a valid budget; simulates install failure
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"

        # Package with malformed max_install_mb → config_errors += 1
        bad_budget = packages_dir / "bad_budget"
        bad_budget.mkdir(parents=True)
        (bad_budget / "pyproject.toml").write_text(
            '[tool.gptme-contrib]\nmax_install_mb = "not-a-number"\n'
        )

        # Package with valid budget but install returns None → install_failures += 1
        broken_install = packages_dir / "broken_install"
        broken_install.mkdir(parents=True)
        (broken_install / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(packages_dir, verbose=False)

    # Both failure classes must be counted independently
    assert result.all_pass is False
    assert result.config_errors == 1
    assert result.install_failures == 1
    assert result.budget_overages == 0
    assert result.pre_measurement_failures == 2  # config_errors + install_failures
    assert result == (
        False,
        2,
        0,
    )  # tuple: (all_pass, pre_measurement_failures, budget_overages)

    # print_failure_summary must emit a distinct line for each class
    check_install_size.print_failure_summary(result)
    out = capsys.readouterr().out
    assert "invalid budget configuration" in out
    assert "failed to install" in out


def test_check_result_is_hashable():
    """CheckResult defines __hash__ so instances can be used in sets and as dict keys."""
    import check_install_size

    r1 = check_install_size.CheckResult(True, 0, 0, 0)
    r2 = check_install_size.CheckResult(False, 1, 0, 0)
    r3 = check_install_size.CheckResult(True, 0, 0, 0)

    # Must not raise TypeError
    result_set = {r1, r2}
    assert r3 in result_set  # r3 equals r1 → same hash, same set membership
    assert r2 not in {r1}
    assert isinstance(hash(r1), int)


def test_check_result_rejects_negative_counts():
    """CheckResult.__init__ raises ValueError for negative counter arguments."""
    import check_install_size
    import pytest

    with pytest.raises(ValueError, match="config_errors"):
        check_install_size.CheckResult(False, -1, 0, 0)
    with pytest.raises(ValueError, match="install_failures"):
        check_install_size.CheckResult(False, 0, -1, 0)
    with pytest.raises(ValueError, match="budget_overages"):
        check_install_size.CheckResult(False, 0, 0, -1)
    with pytest.raises(ValueError, match="resolution_failures"):
        check_install_size.CheckResult(False, 0, 0, 0, resolution_failures=-1)


def test_get_transitive_deps_of_git_packages():
    """_get_transitive_deps_of_git_packages parses uv.lock and returns direct dep names."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = Path(tmpdir) / "uv.lock"
        lock_path.write_text(
            """
[[package]]
name = "gptme"
version = "0.32.1"
source = { git = "https://github.com/gptme/gptme.git?branch=master#abc123" }
dependencies = [
    { name = "anthropic" },
    { name = "click" },
]

[[package]]
name = "anthropic"
version = "0.120.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "click"
version = "8.1.0"
source = { registry = "https://pypi.org/simple" }
""",
            encoding="utf-8",
        )

        deps = check_install_size._get_transitive_deps_of_git_packages(
            lock_path, {"gptme"}
        )

    assert deps == {"anthropic", "click"}


def test_get_transitive_deps_of_git_packages_missing_lock():
    """_get_transitive_deps_of_git_packages returns empty set when lock file missing."""
    import check_install_size

    deps = check_install_size._get_transitive_deps_of_git_packages(
        Path("/nonexistent/uv.lock"), {"gptme"}
    )
    assert deps == set()


def test_get_transitive_deps_of_git_packages_unknown_pkg():
    """_get_transitive_deps_of_git_packages returns empty set when package not in lock."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = Path(tmpdir) / "uv.lock"
        lock_path.write_text(
            '[[package]]\nname = "other"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n',
            encoding="utf-8",
        )
        deps = check_install_size._get_transitive_deps_of_git_packages(
            lock_path, {"gptme"}
        )
    assert deps == set()


def test_get_transitive_deps_of_git_packages_collects_transitives():
    """_get_transitive_deps_of_git_packages follows the dep graph recursively."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = Path(tmpdir) / "uv.lock"
        # git pkg → client → transport (two-hop transitive)
        lock_path.write_text(
            """
[[package]]
name = "mypkg"
version = "1.0"
source = { git = "https://github.com/example/mypkg.git#abc" }
dependencies = [{ name = "client" }]

[[package]]
name = "client"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "transport" }]

[[package]]
name = "transport"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
""",
            encoding="utf-8",
        )
        deps = check_install_size._get_transitive_deps_of_git_packages(
            lock_path, {"mypkg"}
        )

    # Both client (direct) and transport (transitive) must be unpinned.
    assert "client" in deps
    assert "transport" in deps
    # The git package itself is not in the result.
    assert "mypkg" not in deps


def test_measure_install_size_unpins_git_dep_constraints():
    """When git+ lines are dropped, their direct deps are un-pinned in requirements."""
    import check_install_size

    # Simulate uv export returning requirements with a git+ dep and its pinned dep
    file_size = 10 * 1024 * 1024
    seen_req_contents: list[str] = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "venv" in cmd:
            venv_path = Path(cmd[-1])
            _make_fake_venv(venv_path, file_size)
            (venv_path / "bin").mkdir(exist_ok=True)
            (venv_path / "bin" / "python").touch()
            result.stdout = ""
        elif "export" in cmd:
            # Simulates a package with a git dep (gptme) + its pinned transitive dep
            result.stdout = (
                "gptme @ git+https://github.com/gptme/gptme.git@abc123\n"
                "anthropic==0.120.2\n"
                "click==8.1.0\n"
            )
        elif "install" in cmd:
            # Capture the requirements file content written to disk
            req_idx = cmd.index("-r") + 1 if "-r" in cmd else None
            if req_idx:
                seen_req_contents.append(Path(cmd[req_idx]).read_text())
            result.stdout = ""
        elif "du" in cmd:
            result.stdout = f"{file_size}\t{cmd[-1]}\n"
        else:
            result.stdout = ""
        return result

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a fake uv.lock showing gptme's direct deps
        lock_path = Path(tmpdir) / "uv.lock"
        lock_path.write_text(
            """
[[package]]
name = "gptme"
version = "0.32.1"
source = { git = "https://github.com/gptme/gptme.git?branch=master#abc123" }
dependencies = [
    { name = "anthropic" },
]
""",
            encoding="utf-8",
        )
        # package_path.parent.parent must be tmpdir so uv.lock is found there
        pkg_dir = Path(tmpdir) / "packages" / "gptme-voice"
        pkg_dir.mkdir(parents=True)

        with patch.object(subprocess, "run", side_effect=fake_run):
            size = check_install_size.measure_install_size(pkg_dir, "gptme-voice")

    assert isinstance(size, float), f"expected float, got {size!r}"
    assert seen_req_contents, "expected requirements file to be written"
    req_content = seen_req_contents[0]
    # git+ line must be absent
    assert "git+" not in req_content
    # Let the replacement package declare anthropic; do not force a stale root.
    assert "anthropic" not in req_content
    # click was NOT a dep of gptme in the lock, so its pin stays
    assert "click==8.1.0" in req_content


def test_measure_install_size_preserves_env_markers():
    """Removing git deps must not alter retained requirements' markers."""
    import check_install_size

    file_size = 10 * 1024 * 1024
    seen_req_contents: list[str] = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "venv" in cmd:
            venv_path = Path(cmd[-1])
            _make_fake_venv(venv_path, file_size)
            (venv_path / "bin").mkdir(exist_ok=True)
            (venv_path / "bin" / "python").touch()
            result.stdout = ""
        elif "export" in cmd:
            # pywin32 is a Windows-only dep of the git package
            result.stdout = (
                "mypkg @ git+https://github.com/example/mypkg.git@abc\n"
                "pywin32==308 ; sys_platform == 'win32'\n"
                "requests==2.31.0 ; sys_platform == 'linux'\n"
            )
        elif "install" in cmd:
            req_idx = cmd.index("-r") + 1 if "-r" in cmd else None
            if req_idx:
                seen_req_contents.append(Path(cmd[req_idx]).read_text())
            result.stdout = ""
        elif "du" in cmd:
            result.stdout = f"{file_size}\t{cmd[-1]}\n"
        else:
            result.stdout = ""
        return result

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = Path(tmpdir) / "uv.lock"
        lock_path.write_text(
            """
[[package]]
name = "mypkg"
version = "1.0"
source = { git = "https://github.com/example/mypkg.git#abc" }
dependencies = [{ name = "pywin32" }]
""",
            encoding="utf-8",
        )
        pkg_dir = Path(tmpdir) / "packages" / "consumer"
        pkg_dir.mkdir(parents=True)

        with patch.object(subprocess, "run", side_effect=fake_run):
            size = check_install_size.measure_install_size(pkg_dir, "consumer")

    assert isinstance(size, float)
    assert seen_req_contents, "expected requirements file"
    req_content = seen_req_contents[0]
    # No bare Windows dependency remains; retained markers stay intact.
    assert "pywin32" not in req_content
    assert req_content == "requests==2.31.0 ; sys_platform == 'linux'\n"


def test_is_resolution_failure_detects_uv_error():
    """_is_resolution_failure should recognise uv's 'no solution found' output."""
    import check_install_size

    assert check_install_size._is_resolution_failure(
        "error: No solution found when resolving dependencies:\n  Because pkg>=1.0 requires ..."
    )
    assert check_install_size._is_resolution_failure(
        "× No solution found when resolving\n  | Because ..."
    )
    # Generic install errors that are NOT resolution failures
    assert not check_install_size._is_resolution_failure("ERROR: package not found")
    assert not check_install_size._is_resolution_failure("timeout")
    assert not check_install_size._is_resolution_failure("")


def test_measure_install_size_resolution_failure():
    """measure_install_size returns 'resolution_failure' when uv cannot resolve deps."""
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
            result.stderr = (
                "error: No solution found when resolving dependencies:\n"
                "  Because pkg>=2.0 is not available and ..."
            )
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "mypkg"
            package_path.mkdir()
            size = check_install_size.measure_install_size(package_path, "mypkg")

    assert size == "resolution_failure"


def test_check_packages_resolution_failure_counted_separately():
    """check_packages counts resolution failures in resolution_failures, not install_failures."""
    import check_install_size

    def fake_measure(package_path, package_name):
        return "resolution_failure"

    with tempfile.TemporaryDirectory() as tmpdir:
        packages_dir = Path(tmpdir) / "packages"
        pkg_dir = packages_dir / "broken_resolve"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text(
            "[tool.gptme-contrib]\nmax_install_mb = 100\n"
        )

        with patch.object(
            check_install_size, "measure_install_size", side_effect=fake_measure
        ):
            result = check_install_size.check_packages(
                packages_dir, verbose=False, only="broken_resolve"
            )

    assert result.all_pass is False
    assert result.resolution_failures == 1
    assert result.install_failures == 0
    assert result.budget_overages == 0
    # tuple backward compat: pre_measurement_failures includes resolution_failures
    assert result == (False, 1, 0)


def test_print_failure_summary_resolution_failure(capsys):
    """Resolution failures print 'failed to resolve dependencies', not 'failed to install'."""
    import check_install_size

    result = check_install_size.CheckResult(
        all_pass=False,
        config_errors=0,
        install_failures=0,
        budget_overages=0,
        resolution_failures=2,
    )
    check_install_size.print_failure_summary(result)

    out = capsys.readouterr().out
    assert "2 packages failed to resolve dependencies" in out
    assert "failed to install" not in out
    assert "exceeded their install-size budget" not in out


def test_print_failure_summary_no_match_fallback(capsys):
    """When all counts are zero (no-match filter), print_failure_summary emits a brief line."""
    import check_install_size

    # This is the CheckResult returned when --package filter matches nothing
    result = check_install_size.CheckResult(
        all_pass=False, config_errors=0, install_failures=0, budget_overages=0
    )
    check_install_size.print_failure_summary(result)

    out = capsys.readouterr().out
    # Must not be silent — emit at least one line so the CI failure is explained
    assert out.strip()
    # Must not claim a specific failure class that didn't occur
    assert "invalid budget configuration" not in out
    assert "failed to install" not in out
    assert "exceeded" not in out


def test_transitive_deps_union_duplicate_locked_versions(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "mypkg"
dependencies = [{ name = "client", version = "1.0" }]
[[package]]
name = "client"
version = "1.0"
dependencies = [{ name = "transport" }]
[[package]]
name = "client"
version = "2.0"
dependencies = []
[[package]]
name = "transport"
version = "1.0"
""")
    assert check_install_size._get_transitive_deps_of_git_packages(
        lock_path, {"mypkg"}
    ) == {"client", "transport"}


def test_measure_install_size_retains_explicit_artifact_source(tmp_path):
    import check_install_size

    artifact = (
        "transport @ https://example.com/transport-1.0.whl ; python_version >= '3.11'"
    )
    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "mypkg"
dependencies = [{ name = "transport" }]
""")
    pkg_dir = tmp_path / "packages" / "consumer"
    pkg_dir.mkdir(parents=True)
    requirements = []

    def fake_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stderr="", stdout="")
        if "venv" in cmd:
            venv = Path(cmd[-1])
            _make_fake_venv(venv, 1024)
            (venv / "bin").mkdir(exist_ok=True)
            (venv / "bin" / "python").touch()
        elif "export" in cmd:
            result.stdout = (
                "mypkg @ git+https://example.com/mypkg.git@abc\n" + artifact + "\n"
            )
        elif "install" in cmd:
            requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        elif "du" in cmd:
            result.stdout = f"1024\t{cmd[-1]}\n"
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        assert isinstance(
            check_install_size.measure_install_size(pkg_dir, "consumer"), float
        )
    assert requirements == [artifact + "\n"]


def test_transitive_deps_include_optional_edges_and_extras_roots(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "mypkg"
[package.optional-dependencies]
cli = [{ name = "client" }]
[[package]]
name = "client"
dependencies = [{ name = "transport" }]
""")
    assert check_install_size._get_transitive_deps_of_git_packages(
        lock_path, {"mypkg[cli]"}
    ) == {"client", "transport"}


def test_transitive_deps_respect_locked_version_and_source(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
dependencies = [{ name = "client", version = "1.0", source = { registry = "https://pypi.org/simple" } }]
[[package]]
name = "mypkg"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "unrelated-root" }]
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "needed" }]
[[package]]
name = "client"
version = "2.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "unrelated-version" }]
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://private.example/simple" }
dependencies = [{ name = "unrelated-source" }]
""")
    assert check_install_size._get_transitive_deps_of_git_packages(
        lock_path, {"mypkg"}
    ) == {"client", "needed"}


def test_transitive_deps_skip_unrequested_extra(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "mypkg"
dependencies = [{ name = "base" }]
[package.optional-dependencies]
cli = [{ name = "client" }]
[[package]]
name = "client"
dependencies = [{ name = "transport" }]
""")
    assert check_install_size._get_transitive_deps_of_git_packages(
        lock_path, {"mypkg"}
    ) == {"base"}


def test_measure_install_size_tracks_consumer_selected_extras(tmp_path):
    import check_install_size

    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "bridge" }, { name = "unrelated" }]
[[package]]
name = "bridge"
dependencies = [{ name = "mypkg", extra = ["cli"] }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
[package.optional-dependencies]
cli = [{ name = "client", extra = ["http"] }]
unused = [{ name = "unrelated" }]
[[package]]
name = "client"
[package.optional-dependencies]
http = [{ name = "transport" }]
""")
    pkg_dir = tmp_path / "packages" / "consumer"
    pkg_dir.mkdir(parents=True)
    requirements = []

    def fake_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stderr="", stdout="")
        if "venv" in cmd:
            venv = Path(cmd[-1])
            _make_fake_venv(venv, 1024)
            (venv / "bin").mkdir(exist_ok=True)
            (venv / "bin" / "python").touch()
        elif "export" in cmd:
            # uv export does not retain extras in the flattened git requirement.
            result.stdout = (
                "mypkg @ git+https://example.com/mypkg.git@abc\n"
                "client==1.0\ntransport==1.0\nunrelated==1.0\n"
            )
        elif "install" in cmd:
            requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        elif "du" in cmd:
            result.stdout = f"1024\t{cmd[-1]}\n"
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        assert isinstance(
            check_install_size.measure_install_size(pkg_dir, "consumer"), float
        )
    assert requirements == ["unrelated==1.0\n"]


def test_measure_install_size_preserves_independent_locked_version(tmp_path):
    """Only the version reached from the git root loses its exported pin."""
    import check_install_size

    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "mypkg" }, { name = "client", version = "2.0", marker = "sys_platform == 'linux'" }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
dependencies = [{ name = "client", version = "1.0", marker = "sys_platform == 'win32'" }]
[[package]]
name = "client"
version = "1.0"
[[package]]
name = "client"
version = "2.0"
""")
    pkg_dir = tmp_path / "packages" / "consumer"
    pkg_dir.mkdir(parents=True)
    requirements = []

    def fake_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stderr="", stdout="")
        if "venv" in cmd:
            venv = Path(cmd[-1])
            _make_fake_venv(venv, 1024)
            (venv / "bin").mkdir(exist_ok=True)
            (venv / "bin" / "python").touch()
        elif "export" in cmd:
            result.stdout = (
                "mypkg @ git+https://example.com/mypkg.git@abc\n"
                "client==1.0 ; sys_platform == 'win32'\n"
                "client==2.0 ; sys_platform == 'linux'\n"
            )
        elif "install" in cmd:
            requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        elif "du" in cmd:
            result.stdout = f"1024\t{cmd[-1]}\n"
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        assert isinstance(
            check_install_size.measure_install_size(pkg_dir, "consumer"), float
        )
    assert requirements == ["client==2.0 ; sys_platform == 'linux'\n"]


def test_git_dependency_versions_retain_non_pypi_registry_pins(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "consumer-app"
dependencies = [{ name = "mypkg", extra = ["cli"] }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
[package.optional-dependencies]
cli = [{ name = "torch" }, { name = "client" }]
[[package]]
name = "torch"
version = "2.6.0+cpu"
source = { registry = "https://download.pytorch.org/whl/cpu" }
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
""")
    assert check_install_size._get_git_dependency_versions(
        lock_path, {"mypkg"}, "consumer.app"
    ) == {"client": {"1.0"}, "torch": set()}


def test_git_dependency_versions_exclude_unselected_roots_and_source_collisions(
    tmp_path,
):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "mypkg", source = { git = "https://example.com/a.git" } }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/a.git" }
dependencies = [{ name = "client", version = "1.0", source = { registry = "https://pypi.org/simple" } }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/b.git" }
dependencies = [{ name = "unrelated" }]
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://private.example/simple" }
[[package]]
name = "unrelated"
version = "2.0"
""")
    assert check_install_size._get_git_dependency_versions(
        lock_path, {"mypkg"}, "consumer"
    ) == {"client": set()}
    # The source collision is protected even without consumer provenance; root
    # selection is a separate safeguard and must not mask that protection.
    assert check_install_size._get_git_dependency_versions(lock_path, {"mypkg"}) == {
        "client": set(),
        "unrelated": {"2.0"},
    }


def test_measure_install_size_retains_same_version_private_registry_pin(tmp_path):
    import check_install_size

    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "mypkg" }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
dependencies = [
    { name = "client", version = "1.0", source = { registry = "https://pypi.org/simple" }, marker = "sys_platform == 'linux'" },
    { name = "client", version = "1.0", source = { registry = "https://private.example/simple" }, marker = "sys_platform == 'win32'" },
    { name = "public-only", version = "1.0" },
]
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "client"
version = "1.0"
source = { registry = "https://private.example/simple" }
[[package]]
name = "public-only"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
""")
    pkg_dir = tmp_path / "packages" / "consumer"
    pkg_dir.mkdir(parents=True)
    requirements = []

    def fake_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stderr="", stdout="")
        if "venv" in cmd:
            venv = Path(cmd[-1])
            _make_fake_venv(venv, 1024)
            (venv / "bin").mkdir(exist_ok=True)
            (venv / "bin" / "python").touch()
        elif "export" in cmd:
            result.stdout = (
                "mypkg @ git+https://example.com/mypkg.git@abc\n"
                "client==1.0 ; sys_platform == 'linux'\n"
                "client==1.0 ; sys_platform == 'win32'\n"
                "public-only==1.0\n"
            )
        elif "install" in cmd:
            requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        elif "du" in cmd:
            result.stdout = f"1024\t{cmd[-1]}\n"
        return result

    with patch.object(subprocess, "run", side_effect=fake_run):
        assert isinstance(
            check_install_size.measure_install_size(pkg_dir, "consumer"), float
        )
    assert requirements == [
        "client==1.0 ; sys_platform == 'linux'\nclient==1.0 ; sys_platform == 'win32'\n"
    ]


def test_transitive_deps_include_independently_selected_descendant_extra(tmp_path):
    import check_install_size

    lock_path = tmp_path / "uv.lock"
    lock_path.write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "mypkg" }, { name = "client", extra = ["http"] }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
dependencies = [{ name = "client" }]
[[package]]
name = "client"
[package.optional-dependencies]
http = [{ name = "transport" }]
""")
    assert check_install_size._get_transitive_deps_of_git_packages(
        lock_path, {"mypkg"}, "consumer"
    ) == {"client", "transport"}


def test_measure_install_size_allows_obsolete_git_dependencies_to_disappear(tmp_path):
    """Run uv's offline resolver: obsolete roots must not force a conflict."""
    import shutil
    import zipfile

    import check_install_size
    import pytest

    if not shutil.which("uv"):
        pytest.skip("uv is required for the offline resolver regression")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name, version, dependencies in [
        ("mypkg", "2.0", ["shared>=2"]),
        ("old_client", "1.0", ["shared<2"]),
        ("shared", "1.0", []),
        ("shared", "2.0", []),
        ("independent", "1.0", ["shared>=1,<3"]),
    ]:
        dist_info = f"{name}-{version}.dist-info"
        with zipfile.ZipFile(
            wheels / f"{name}-{version}-py3-none-any.whl", "w"
        ) as wheel:
            wheel.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                + "".join(f"Requires-Dist: {dep}\n" for dep in dependencies),
            )
            wheel.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(f"{dist_info}/RECORD", "")
    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "consumer"
dependencies = [{ name = "mypkg" }, { name = "independent" }]
[[package]]
name = "mypkg"
source = { git = "https://example.com/mypkg.git" }
dependencies = [{ name = "old-client" }]
[[package]]
name = "old-client"
version = "1.0"
dependencies = [{ name = "shared" }]
[[package]]
name = "shared"
version = "1.0"
[[package]]
name = "independent"
version = "1.0"
dependencies = [{ name = "shared" }]
""")
    pkg_dir = tmp_path / "packages" / "consumer"
    pkg_dir.mkdir(parents=True)
    real_run = subprocess.run
    resolutions = []

    def fake_run(cmd, **kwargs):
        if "venv" in cmd:
            venv = Path(cmd[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").touch()
        if "export" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "mypkg @ git+https://example.com/mypkg.git@abc\n"
                "old-client==1.0\nshared==1.0\nindependent==1.0\n",
                "",
            )
        if "install" in cmd:
            requirements = Path(cmd[cmd.index("-r") + 1])
            # The consumer independently declares these requirements. Resolve them
            # together with the checker's transformed export, without network/builds.
            combined = tmp_path / "combined.txt"
            combined.write_text(requirements.read_text() + "mypkg==2.0\n")
            result = real_run(
                [
                    "uv",
                    "pip",
                    "compile",
                    "--offline",
                    "--no-index",
                    "--no-config",
                    "--find-links",
                    str(wheels),
                    str(combined),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            resolutions.append(result)
            return result
        if "du" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"1024\t{cmd[-1]}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(subprocess, "run", side_effect=fake_run):
        size = check_install_size.measure_install_size(pkg_dir, "consumer")
    assert resolutions, "expected uv resolver to run"
    assert resolutions[0].returncode == 0, resolutions[0].stderr
    assert isinstance(size, float)
    resolved = resolutions[0].stdout
    assert "old-client" not in resolved
    assert "shared==2.0" in resolved
    assert "independent==1.0" in resolved
