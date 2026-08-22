#!/usr/bin/env python3
"""
Validator for: Execute Scripts With Shebangs
Lesson: lessons/tools/execute-scripts-with-shebangs.md

Detects when scripts with shebangs are invoked through an interpreter instead
of being executed directly, which can bypass important execution context.

Real incident:
- Failed: python3 tools/rss_reader.py (bypassed #!/usr/bin/env -S uv run)
- Result: ModuleNotFoundError due to missing uv environment
- Fixed: ./tools/rss_reader.py (honored shebang, loaded dependencies)

Usage:
    python3 validate_execute_scripts_with_shebangs.py file1.md file2.sh ...
    python3 validate_execute_scripts_with_shebangs.py --strict file1.md ...
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
LESSON_PATH = "lessons/tools/execute-scripts-with-shebangs.md"
LESSON_NAME = "Execute Scripts With Shebangs"
FILE_PATTERNS = [".md", ".sh", ".bash"]

# Patterns for interpreter invocation: interpreter path/to/script
INTERPRETER_PATTERNS = [
    r"(?<!\S)python3\s+([^\s|&;]+\.py)",
    r"(?<!\S)python\s+([^\s|&;]+\.py)",
    r"(?<!\S)bash\s+([^\s|&;]+\.(?:sh|bash))",
    r"(?<!\S)sh\s+([^\s|&;]+\.sh)",
    r"(?<!\S)zsh\s+([^\s|&;]+\.zsh)",
]

# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class ExecuteScriptsWithShebangsValidator:
    """Validates script execution respects shebangs."""

    def __init__(self, strict: bool = False, verbose: bool = False):
        self.strict = strict
        self.verbose = verbose
        self.violations: List[Tuple[Path, int, str, str, str]] = []

    def should_check_file(self, filepath: Path) -> bool:
        """Check if file should be validated."""
        # Skip lesson files - they need to show anti-patterns
        if "lessons/" in str(filepath):
            return False
        # Skip test files
        if "tests/" in str(filepath) or "test_" in filepath.name:
            return False
        return filepath.suffix in FILE_PATTERNS

    def has_shebang(self, script_path: Path) -> str | None:
        """Check if script has a shebang, return the shebang line if found."""
        if not script_path.exists():
            return None

        try:
            with open(script_path, encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
                if first_line.startswith("#!"):
                    return first_line
        except Exception:
            pass

        return None

    def resolve_script_path(self, base_path: Path, script_ref: str) -> Path | None:
        """Resolve script path relative to base file."""
        # Try absolute path first
        script_path = Path(script_ref)
        if script_path.is_absolute() and script_path.exists():
            return script_path

        # Try relative to base file's directory
        base_dir = base_path.parent
        script_path = base_dir / script_ref
        if script_path.exists():
            return script_path

        # Try relative to workspace root
        script_path = WORKSPACE_ROOT / script_ref
        if script_path.exists():
            return script_path

        return None

    def validate_file(self, filepath: Path) -> bool:
        """Validate a single file for script execution patterns."""
        try:
            content = filepath.read_text()
        except Exception as e:
            if self.verbose:
                print(f"Error reading {filepath}: {e}")
            return True

        lines = content.split("\n")
        violations_found = False
        in_code_block = False
        code_block_type = None

        seen_violations: set[tuple[int, str]] = set()

        for line_num, line in enumerate(lines, start=1):
            # Track code blocks in markdown
            if filepath.suffix == ".md":
                if line.strip().startswith("```"):
                    if not in_code_block:
                        in_code_block = True
                        # Extract code block type
                        code_block_type = (
                            line.strip()[3:].split()[0] if len(line.strip()) > 3 else ""
                        )
                    else:
                        in_code_block = False
                        code_block_type = None
                    continue
                # Only check shell code blocks
                if not in_code_block or code_block_type not in [
                    "shell",
                    "bash",
                    "sh",
                    "",
                ]:
                    continue

            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            # Check for interpreter invocation patterns
            for pattern in INTERPRETER_PATTERNS:
                match = re.search(pattern, line)
                if match:
                    script_ref = match.group(1)
                    script_path = self.resolve_script_path(filepath, script_ref)

                    if script_path:
                        shebang = self.has_shebang(script_path)
                        violation_key = (line_num, script_ref)
                        if shebang and violation_key not in seen_violations:
                            self.violations.append(
                                (filepath, line_num, line.strip(), script_ref, shebang)
                            )
                            seen_violations.add(violation_key)
                            violations_found = True

        return not violations_found

    def get_error_message(
        self, filepath: Path, line_num: int, line: str, script_ref: str, shebang: str
    ) -> str:
        """Get actionable error message for a violation."""
        # Extract interpreter from shebang for context
        shebang_info = shebang.split()
        interpreter_info = (
            " ".join(shebang_info[:3]) if len(shebang_info) > 2 else shebang
        )

        return (
            f"Line {line_num}: Script invoked through interpreter bypasses shebang\n"
            f"    Command: {line}\n"
            f"    Script: {script_ref}\n"
            f"    Shebang: {interpreter_info}\n"
            f"    Risk: Bypassing shebang loses execution context:\n"
            f"      - Dependency isolation (uv run, poetry run)\n"
            f"      - Interpreter flags (-u, -O)\n"
            f"      - Environment setup\n"
            f"    Fix: Make script executable and run directly:\n"
            f"      chmod +x {script_ref}\n"
            f"      ./{script_ref}\n"
        )

    def run(self, files: List[Path]) -> int:
        """Run validator on list of files."""
        if self.verbose:
            print(f"Validating {len(files)} files for script execution patterns")

        for filepath in files:
            if not filepath.exists():
                if self.verbose:
                    print(f"Skipping {filepath}: file does not exist")
                continue

            if not self.should_check_file(filepath):
                if self.verbose:
                    print(f"Skipping {filepath}: not in FILE_PATTERNS")
                continue

            self.validate_file(filepath)

        # Report violations
        if self.violations:
            level = "ERROR" if self.strict else "WARNING"
            print(f"\n{level}: {LESSON_NAME} validation failed")
            print(f"Lesson: {LESSON_PATH}\n")

            for filepath, line_num, line, script_ref, shebang in self.violations:
                msg = self.get_error_message(
                    filepath, line_num, line, script_ref, shebang
                )
                print(f"  {filepath}")
                print(f"    {msg}\n")

            print(f"Found {len(self.violations)} violation(s)")
            print("\nInvoking scripts through interpreters bypasses shebangs, causing:")
            print("  - Import errors from missing dependencies (ModuleNotFoundError)")
            print("  - Wrong Python version or environment")
            print("  - Missing interpreter flags (-u for unbuffered, etc.)")
            print("  - Broken dependency isolation (uv run, poetry run)")
            print(
                "\nReal incident: python3 tools/rss_reader.py bypassed uv environment"
            )

            if not self.strict:
                print("\nThese are warnings. Use --strict to fail on violations.")

            return 1 if self.strict else 0

        if self.verbose:
            print("✓ All script invocations respect shebangs")

        return 0


# ==============================================================================
# CLI INTERFACE
# ==============================================================================


def main():
    """Main entry point for validator."""
    parser = argparse.ArgumentParser(
        description="Validate script execution respects shebangs",
        epilog=f"Lesson: {LESSON_PATH}",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to validate")
    parser.add_argument(
        "--strict", action="store_true", help="Fail with error exit code on violations"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print detailed information"
    )

    args = parser.parse_args()

    validator = ExecuteScriptsWithShebangsValidator(
        strict=args.strict, verbose=args.verbose
    )

    exit_code = validator.run(args.files)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
