"""Tests for the install size checker script."""

import sys
import tempfile
from pathlib import Path

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
    """Test that pyproject without [tool.gptme-contrib] returns default."""
    import check_install_size

    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject_path = Path(tmpdir) / "pyproject.toml"
        pyproject_path.write_text(
            """
[project]
name = "test"
"""
        )

        budget = check_install_size.get_package_budget(pyproject_path)
        assert budget == check_install_size.DEFAULT_BUDGETS["other"]
