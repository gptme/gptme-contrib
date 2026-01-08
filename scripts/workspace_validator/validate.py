#!/usr/bin/env python3
"""
Agent Workspace Validator

Validates that a directory conforms to the gptme agent workspace structure.
Used by agent template CI and agent forks to validate their workspaces.

Usage:
    python validate.py [--workspace PATH] [--check CHECKS]

Examples:
    python validate.py                          # Validate current directory
    python validate.py --workspace /path/to/ws  # Validate specific workspace
    python validate.py --check files,dirs       # Run specific checks only
"""

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


# Required files for a valid agent workspace
REQUIRED_FILES = [
    "gptme.toml",
    "ABOUT.md",
    "README.md",
]

# Optional but recommended files
RECOMMENDED_FILES = [
    "ARCHITECTURE.md",
    "TASKS.md",
    "GLOSSARY.md",
]

# Required directories for a valid agent workspace
REQUIRED_DIRS = [
    "journal",
    "knowledge",
    "lessons",
    "tasks",
]

# Optional but recommended directories
RECOMMENDED_DIRS = [
    "people",
    "tools",
    "scripts",
]


class ValidationResult:
    """Result of a validation check."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def add_error(self, msg: str):
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def add_info(self, msg: str):
        self.info.append(msg)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def merge(self, other: "ValidationResult"):
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.info.extend(other.info)


def check_required_files(workspace: Path) -> ValidationResult:
    """Check that required files exist."""
    result = ValidationResult()

    for filename in REQUIRED_FILES:
        filepath = workspace / filename
        if filepath.exists():
            result.add_info(f"✅ Required file exists: {filename}")
        else:
            result.add_error(f"❌ Missing required file: {filename}")

    for filename in RECOMMENDED_FILES:
        filepath = workspace / filename
        if filepath.exists():
            result.add_info(f"✅ Recommended file exists: {filename}")
        else:
            result.add_warning(f"⚠️  Missing recommended file: {filename}")

    return result


def check_required_dirs(workspace: Path) -> ValidationResult:
    """Check that required directories exist."""
    result = ValidationResult()

    for dirname in REQUIRED_DIRS:
        dirpath = workspace / dirname
        if dirpath.is_dir():
            result.add_info(f"✅ Required directory exists: {dirname}/")
        else:
            result.add_error(f"❌ Missing required directory: {dirname}/")

    for dirname in RECOMMENDED_DIRS:
        dirpath = workspace / dirname
        if dirpath.is_dir():
            result.add_info(f"✅ Recommended directory exists: {dirname}/")
        else:
            result.add_warning(f"⚠️  Missing recommended directory: {dirname}/")

    return result


def check_gptme_toml(workspace: Path) -> ValidationResult:
    """Validate gptme.toml configuration."""
    result = ValidationResult()
    config_path = workspace / "gptme.toml"

    if not config_path.exists():
        result.add_error("❌ gptme.toml not found (required for validation)")
        return result

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception as e:
        result.add_error(f"❌ Failed to parse gptme.toml: {e}")
        return result

    # Check [agent] section
    if "agent" in config:
        result.add_info("✅ [agent] section present")
        if "name" in config["agent"]:
            result.add_info(f"✅ Agent name: {config['agent']['name']}")
        else:
            result.add_warning("⚠️  [agent] section missing 'name' field")
    else:
        result.add_error("❌ Missing [agent] section in gptme.toml")

    # Check [prompt] section
    if "prompt" in config:
        result.add_info("✅ [prompt] section present")

        # Check referenced files exist
        if "files" in config["prompt"]:
            for ref_file in config["prompt"]["files"]:
                # Skip submodule files (may not be initialized)
                if ref_file.startswith("gptme-contrib/"):
                    result.add_info(f"⏭️  Skipping submodule file: {ref_file}")
                    continue

                ref_path = workspace / ref_file
                if ref_path.exists():
                    result.add_info(f"✅ Referenced file exists: {ref_file}")
                else:
                    result.add_error(f"❌ Missing referenced file: {ref_file}")
    else:
        result.add_warning("⚠️  Missing [prompt] section in gptme.toml")

    return result


def check_fork_script(workspace: Path) -> ValidationResult:
    """Check fork.sh script if present."""
    result = ValidationResult()
    fork_script = workspace / "fork.sh"

    if not fork_script.exists():
        result.add_warning("⚠️  No fork.sh script (optional for forkable templates)")
        return result

    if not fork_script.stat().st_mode & 0o111:
        result.add_error("❌ fork.sh exists but is not executable")
    else:
        result.add_info("✅ fork.sh is executable")

    return result


def validate_workspace(
    workspace: Path,
    checks: list[str] | None = None,
) -> ValidationResult:
    """
    Validate an agent workspace.

    Args:
        workspace: Path to the workspace root
        checks: Optional list of specific checks to run.
                If None, runs all checks.
                Valid checks: files, dirs, config, fork

    Returns:
        ValidationResult with all errors, warnings, and info messages.
    """
    result = ValidationResult()

    available_checks = {
        "files": check_required_files,
        "dirs": check_required_dirs,
        "config": check_gptme_toml,
        "fork": check_fork_script,
    }

    if checks is None:
        checks = list(available_checks.keys())

    for check_name in checks:
        if check_name in available_checks:
            check_result = available_checks[check_name](workspace)
            result.merge(check_result)
        else:
            result.add_warning(f"⚠️  Unknown check: {check_name}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Validate a gptme agent workspace structure"
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=Path,
        default=Path.cwd(),
        help="Path to workspace root (default: current directory)",
    )
    parser.add_argument(
        "--check",
        "-c",
        type=str,
        default=None,
        help="Comma-separated list of checks to run (files,dirs,config,fork)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only show errors and warnings",
    )

    args = parser.parse_args()
    workspace = args.workspace.resolve()

    checks = args.check.split(",") if args.check else None

    print(f"Validating workspace: {workspace}\n")

    result = validate_workspace(workspace, checks)

    # Print results
    if not args.quiet:
        for msg in result.info:
            print(msg)
        print()

    if result.warnings:
        print("Warnings:")
        for msg in result.warnings:
            print(f"  {msg}")
        print()

    if result.errors:
        print("Errors:")
        for msg in result.errors:
            print(f"  {msg}")
        print()

    # Summary
    if result.passed:
        print("✅ Workspace validation PASSED")
        if result.warnings:
            print(f"   ({len(result.warnings)} warnings)")
        return 0
    else:
        print(f"❌ Workspace validation FAILED ({len(result.errors)} errors)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
