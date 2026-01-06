"""Tests for template validation check_names module."""
import tempfile
from pathlib import Path

import pytest

from template_validation.check_names import (
    validate_names,
    validate_agent_identity,
    should_exclude,
    TEMPLATE_PATTERNS,
    FORK_PATTERNS,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        yield root


def test_should_exclude():
    """Test file exclusion logic."""
    # Directory exclusion
    assert should_exclude(Path("docs/file.md"), ["docs/"])
    assert should_exclude(Path("knowledge/lesson.md"), ["knowledge/"])
    
    # Glob pattern
    assert should_exclude(Path("README.md"), ["*.md"])
    assert should_exclude(Path("dir/file.md"), ["*.md"])
    
    # No exclusion
    assert not should_exclude(Path("src/main.py"), ["docs/", "*.md"])


def test_template_mode_detects_incomplete_agent_name(temp_repo):
    """Test template mode detects incomplete agent references."""
    # Create file with incomplete agent name
    file_path = temp_repo / "test.py"
    file_path.write_text("# This is for gptme-agent usage\n")
    
    result = validate_names(temp_repo, mode="template")
    
    assert not result.is_valid()
    assert len(result.violations) == 1
    assert result.violations[0]["pattern"] == "gptme-agent-incomplete"


def test_template_mode_allows_full_template_name(temp_repo):
    """Test template mode allows gptme-agent-template."""
    file_path = temp_repo / "test.py"
    file_path.write_text("# Part of gptme-agent-template\n")
    
    result = validate_names(temp_repo, mode="template")
    
    assert result.is_valid()


def test_template_mode_detects_placeholders(temp_repo):
    """Test template mode detects placeholder names."""
    file_path = temp_repo / "config.toml"
    file_path.write_text('name = "agent-name"\n')
    
    result = validate_names(temp_repo, mode="template")
    
    assert not result.is_valid()
    assert any(v["pattern"] == "agent-name-placeholder" for v in result.violations)


def test_fork_mode_detects_template_references(temp_repo):
    """Test fork mode detects template references in code."""
    file_path = temp_repo / "main.py"
    file_path.write_text("# Forked from gptme-agent-template\n")
    
    result = validate_names(temp_repo, mode="fork")
    
    assert not result.is_valid()
    assert result.violations[0]["pattern"] == "template-reference"


def test_fork_mode_excludes_documentation(temp_repo):
    """Test fork mode excludes documentation directories."""
    # Create docs directory with template reference
    docs_dir = temp_repo / "docs"
    docs_dir.mkdir()
    (docs_dir / "README.md").write_text("Forked from gptme-agent-template\n")
    
    result = validate_names(temp_repo, mode="fork")
    
    # Should be valid because docs/ is excluded in fork mode
    assert result.is_valid()


def test_fork_mode_excludes_markdown_files(temp_repo):
    """Test fork mode excludes markdown files."""
    (temp_repo / "ABOUT.md").write_text("Forked from gptme-agent-template\n")
    
    result = validate_names(temp_repo, mode="fork")
    
    # Should be valid because *.md is excluded in fork mode
    assert result.is_valid()


def test_custom_excludes(temp_repo):
    """Test custom exclusion patterns."""
    file_path = temp_repo / "custom.txt"
    file_path.write_text("gptme-agent-template\n")
    
    # Without exclusion, should fail
    result = validate_names(temp_repo, mode="fork")
    assert not result.is_valid()
    
    # With exclusion, should pass
    result = validate_names(temp_repo, mode="fork", excludes=["custom.txt"])
    assert result.is_valid()


def test_custom_patterns(temp_repo):
    """Test custom validation patterns."""
    file_path = temp_repo / "test.py"
    file_path.write_text("CUSTOM_PATTERN_HERE\n")
    
    custom = {"custom-test": r"CUSTOM_PATTERN"}
    result = validate_names(temp_repo, mode="fork", custom_patterns=custom)
    
    assert not result.is_valid()
    assert result.violations[0]["pattern"] == "custom-test"


def test_validate_agent_identity_missing_gptme_toml(temp_repo):
    """Test agent identity validation with missing gptme.toml."""
    errors = validate_agent_identity(temp_repo)
    
    assert len(errors) > 0
    assert any("gptme.toml" in e for e in errors)


def test_validate_agent_identity_valid(temp_repo):
    """Test agent identity validation with valid config."""
    # Create valid gptme.toml
    (temp_repo / "gptme.toml").write_text('[agent]\nname = "MyAgent"\n')
    
    # Create valid ABOUT.md
    (temp_repo / "ABOUT.md").write_text("# About MyAgent\n\nThis is MyAgent.\n")
    
    errors = validate_agent_identity(temp_repo)
    
    assert len(errors) == 0


def test_validate_agent_identity_default_name(temp_repo):
    """Test agent identity validation detects default names."""
    (temp_repo / "gptme.toml").write_text('[agent]\nname = "agent-name"\n')
    (temp_repo / "ABOUT.md").write_text("# About Agent\n")
    
    errors = validate_agent_identity(temp_repo)
    
    assert len(errors) > 0
    assert any("default agent name" in e for e in errors)


def test_validate_agent_identity_placeholder_in_about(temp_repo):
    """Test agent identity validation detects placeholders in ABOUT.md."""
    (temp_repo / "gptme.toml").write_text('[agent]\nname = "MyAgent"\n')
    (temp_repo / "ABOUT.md").write_text("# About [AGENT_NAME]\n")
    
    errors = validate_agent_identity(temp_repo)
    
    assert len(errors) > 0
    assert any("placeholder" in e for e in errors)


def test_validation_result_format_report(temp_repo):
    """Test validation result report formatting."""
    file_path = temp_repo / "test.py"
    file_path.write_text("gptme-agent\n")
    
    result = validate_names(temp_repo, mode="template")
    report = result.format_report()
    
    assert "✗" in report
    assert "violations" in report
    assert "test.py" in report


def test_validation_result_format_report_success(temp_repo):
    """Test validation result report for success."""
    (temp_repo / "test.py").write_text("# Clean file\n")
    
    result = validate_names(temp_repo, mode="fork")
    report = result.format_report()
    
    assert "✓" in report
    assert "Validation passed" in report


def test_template_patterns():
    """Test that template patterns are defined correctly."""
    assert "gptme-agent-incomplete" in TEMPLATE_PATTERNS
    assert "agent-name-placeholder" in TEMPLATE_PATTERNS
    assert len(TEMPLATE_PATTERNS) > 0


def test_fork_patterns():
    """Test that fork patterns are defined correctly."""
    assert "template-reference" in FORK_PATTERNS
    assert "template-suffix" in FORK_PATTERNS
    assert len(FORK_PATTERNS) > 0
