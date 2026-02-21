"""
Integration tests for the workspace validator.

Tests the validation logic against mock workspaces.
"""

import sys
import tempfile
from pathlib import Path

import pytest

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from workspace_validator import (
    REQUIRED_DIRS,
    REQUIRED_FILES,
    ValidationResult,
    check_fork_script,
    check_gptme_toml,
    check_required_dirs,
    check_required_files,
    validate_workspace,
)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def valid_workspace(temp_workspace):
    """Create a valid minimal workspace."""
    # Create required files
    for filename in REQUIRED_FILES:
        (temp_workspace / filename).write_text(f"# {filename}\n")

    # Create required directories
    for dirname in REQUIRED_DIRS:
        (temp_workspace / dirname).mkdir()

    # Create minimal gptme.toml
    gptme_toml = temp_workspace / "gptme.toml"
    gptme_toml.write_text("""
[agent]
name = "TestAgent"

[prompt]
files = ["README.md", "ABOUT.md"]
""")

    return temp_workspace


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_empty_result_passes(self):
        result = ValidationResult()
        assert result.passed
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_result_with_errors_fails(self):
        result = ValidationResult()
        result.add_error("Test error")
        assert not result.passed
        assert len(result.errors) == 1

    def test_result_with_warnings_passes(self):
        result = ValidationResult()
        result.add_warning("Test warning")
        assert result.passed
        assert len(result.warnings) == 1

    def test_merge_results(self):
        result1 = ValidationResult()
        result1.add_error("Error 1")
        result1.add_warning("Warning 1")

        result2 = ValidationResult()
        result2.add_error("Error 2")
        result2.add_info("Info 1")

        result1.merge(result2)

        assert len(result1.errors) == 2
        assert len(result1.warnings) == 1
        assert len(result1.info) == 1


class TestCheckRequiredFiles:
    """Tests for file validation."""

    def test_missing_all_files(self, temp_workspace):
        result = check_required_files(temp_workspace)
        assert not result.passed
        assert len(result.errors) == len(REQUIRED_FILES)

    def test_all_required_files_present(self, valid_workspace):
        result = check_required_files(valid_workspace)
        # Should pass (no errors for required files)
        required_errors = [e for e in result.errors if "Missing required file" in e]
        assert len(required_errors) == 0

    def test_partial_files(self, temp_workspace):
        # Create only some required files
        (temp_workspace / "gptme.toml").write_text("[agent]\nname = 'Test'\n")
        result = check_required_files(temp_workspace)
        assert not result.passed
        # Should have errors for missing ABOUT.md, README.md
        assert len(result.errors) == len(REQUIRED_FILES) - 1


class TestCheckRequiredDirs:
    """Tests for directory validation."""

    def test_missing_all_dirs(self, temp_workspace):
        result = check_required_dirs(temp_workspace)
        assert not result.passed
        assert len(result.errors) == len(REQUIRED_DIRS)

    def test_all_required_dirs_present(self, valid_workspace):
        result = check_required_dirs(valid_workspace)
        required_errors = [
            e for e in result.errors if "Missing required directory" in e
        ]
        assert len(required_errors) == 0

    def test_file_not_dir(self, temp_workspace):
        # Create a file where directory should be
        (temp_workspace / "journal").write_text("not a directory")
        result = check_required_dirs(temp_workspace)
        assert not result.passed


class TestCheckGptmeToml:
    """Tests for gptme.toml validation."""

    def test_missing_config(self, temp_workspace):
        result = check_gptme_toml(temp_workspace)
        assert not result.passed
        assert any("gptme.toml not found" in e for e in result.errors)

    def test_invalid_toml(self, temp_workspace):
        (temp_workspace / "gptme.toml").write_text("invalid toml {{{")
        result = check_gptme_toml(temp_workspace)
        assert not result.passed
        assert any("Failed to parse" in e for e in result.errors)

    def test_missing_agent_section(self, temp_workspace):
        (temp_workspace / "gptme.toml").write_text("[prompt]\nfiles = []\n")
        result = check_gptme_toml(temp_workspace)
        assert not result.passed
        assert any("Missing [agent] section" in e for e in result.errors)

    def test_valid_config(self, valid_workspace):
        result = check_gptme_toml(valid_workspace)
        assert result.passed

    def test_missing_referenced_file(self, temp_workspace):
        (temp_workspace / "gptme.toml").write_text("""
[agent]
name = "Test"

[prompt]
files = ["nonexistent.md"]
""")
        result = check_gptme_toml(temp_workspace)
        assert not result.passed
        assert any("Missing referenced file" in e for e in result.errors)

    def test_submodule_files_skipped(self, temp_workspace):
        """Files in gptme-contrib/ should be skipped (submodule may not be init)."""
        (temp_workspace / "gptme.toml").write_text("""
[agent]
name = "Test"

[prompt]
files = ["gptme-contrib/lessons/something.md"]
""")
        result = check_gptme_toml(temp_workspace)
        # Should pass - submodule files are skipped
        assert result.passed
        assert any("Skipping submodule file" in i for i in result.info)


class TestCheckForkScript:
    """Tests for fork.sh validation."""

    def test_no_fork_script(self, temp_workspace):
        result = check_fork_script(temp_workspace)
        # Should warn, not error
        assert result.passed
        assert len(result.warnings) == 1

    def test_non_executable_fork_script(self, temp_workspace):
        fork_script = temp_workspace / "fork.sh"
        fork_script.write_text("#!/bin/bash\necho 'fork'\n")
        fork_script.chmod(0o644)  # Not executable
        result = check_fork_script(temp_workspace)
        assert not result.passed
        assert any("not executable" in e for e in result.errors)

    def test_executable_fork_script(self, temp_workspace):
        fork_script = temp_workspace / "fork.sh"
        fork_script.write_text("#!/bin/bash\necho 'fork'\n")
        fork_script.chmod(0o755)  # Executable
        result = check_fork_script(temp_workspace)
        assert result.passed


class TestValidateWorkspace:
    """Tests for the main validation function."""

    def test_valid_workspace_passes(self, valid_workspace):
        result = validate_workspace(valid_workspace)
        assert result.passed

    def test_empty_workspace_fails(self, temp_workspace):
        result = validate_workspace(temp_workspace)
        assert not result.passed

    def test_specific_checks_only(self, temp_workspace):
        # Create only required files
        for filename in REQUIRED_FILES:
            (temp_workspace / filename).write_text(f"# {filename}\n")
        (temp_workspace / "gptme.toml").write_text("[agent]\nname = 'Test'\n")

        # Run only files check
        result = validate_workspace(temp_workspace, checks=["files"])
        assert result.passed  # Files exist

        # Full validation should fail (missing dirs)
        result = validate_workspace(temp_workspace)
        assert not result.passed

    def test_unknown_check_warns(self, valid_workspace):
        result = validate_workspace(valid_workspace, checks=["unknown_check"])
        assert any("Unknown check" in w for w in result.warnings)
