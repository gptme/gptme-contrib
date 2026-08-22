#!/usr/bin/env python3
"""
Validator for: Python Invocation
Lesson: lessons/tools/python-invocation.md

Ensures 'python3' is used explicitly instead of 'python' in shell commands,
scripts, and subprocess calls.

Usage:
    python3 validate_python_invocation.py file1.sh file2.py ...
    python3 validate_python_invocation.py --strict file1.sh file2.py ...
"""

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
# The python-invocation lesson is deduplicated into gptme-contrib (the local
# lessons/tools/ copy was removed as an identical duplicate in e661c5e010), so
# point users at the canonical shared-infra location, not a dead local path.
LESSON_PATH = "gptme-contrib/lessons/tools/python-invocation.md"
LESSON_NAME = "Python Invocation"
FILE_PATTERNS = [".sh", ".bash", ".py", ".md"]
SHELL_KEYWORDS = {"if", "then", "elif", "else", "fi", "while", "until", "do", "done"}
SHELL_COMMAND_RESETS = {"&&", "||", "|", ";"}
SHELL_WRAPPER_COMMANDS = {"env", "sudo", "nohup", "time", "nice"}
SHELL_PROBE_COMMANDS = {"command", "which", "type"}
SHELL_WRAPPER_OPTIONS_WITH_VALUE = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "--argv0"},
    "sudo": {
        "-C",
        "-D",
        "-g",
        "-h",
        "-p",
        "-R",
        "-r",
        "-t",
        "-T",
        "-u",
        "-U",
        "--chdir",
        "--close-from",
        "--group",
        "--host",
        "--other-user",
        "--prompt",
        "--role",
        "--type",
        "--user",
    },
    "nice": {"-n", "--adjustment"},
}
COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(\s*python(?:\s|[);]|$)|`\s*python(?:\s|$)")


# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class PythonInvocationValidator:
    """Validates explicit python3 usage instead of python."""

    def __init__(self, strict: bool = False, verbose: bool = False):
        self.strict = strict
        self.verbose = verbose
        self.violations: List[Tuple[Path, int, str, str]] = []

    def should_check_file(self, filepath: Path) -> bool:
        """Check if file should be validated."""
        # Skip lesson files (they show anti-patterns)
        if "lessons/" in str(filepath):
            return False
        return filepath.suffix in FILE_PATTERNS

    def check_shebang(self, filepath: Path, line: str, line_num: int) -> bool:
        """Check if shebang uses python instead of python3."""
        if line.startswith("#!") and "python" in line and "python3" not in line:
            # Found shebang with 'python' but not 'python3'
            self.violations.append(
                (
                    filepath,
                    line_num,
                    "shebang",
                    f"Use '#!/usr/bin/env python3' instead of '{line.strip()}'",
                )
            )
            return False
        return True

    def check_shell_command(self, filepath: Path, line: str, line_num: int) -> bool:
        """Check shell commands for 'python ' usage."""
        # Skip comments
        if line.strip().startswith("#"):
            return True

        if self._shell_invokes_python(line):
            self.violations.append(
                (
                    filepath,
                    line_num,
                    "shell_command",
                    f"Use 'python3' instead of 'python' in command: {line.strip()}",
                )
            )
            return False
        return True

    def _shell_tokens(self, line: str) -> list[str]:
        """Return normalized shell-like tokens for command detection."""
        try:
            tokens = shlex.split(line, comments=False, posix=True)
        except ValueError:
            return []
        return [token.strip("(){}[];|&") for token in tokens]

    def _is_assignment_token(self, token: str) -> bool:
        """Return True for shell-style environment assignments."""
        if "=" not in token or token.startswith(("=", "-", "./", "/")):
            return False
        name, _, _ = token.partition("=")
        return bool(name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))

    def _wrapper_consumes_token(self, wrapper: str, token: str) -> bool:
        """Return True while a wrapper is still parsing its own flags/assignments."""
        if wrapper == "env":
            return token.startswith("-") or self._is_assignment_token(token)
        return token.startswith("-")

    def _wrapper_option_arg_count(self, wrapper: str, token: str) -> int:
        """Return the number of following tokens a wrapper option consumes."""
        if token in SHELL_WRAPPER_OPTIONS_WITH_VALUE.get(wrapper, set()):
            return 1
        return 0

    def _shell_invokes_python(self, line: str) -> bool:
        """Return True when a shell line executes or explicitly probes `python`."""
        if COMMAND_SUBSTITUTION_RE.search(line):
            return True

        try:
            tokens = shlex.split(line, comments=False, posix=True)
        except ValueError:
            return False

        expect_command = True
        active_probe = ""
        active_wrapper = ""
        wrapper_option_arg_pending = 0
        for raw_token in tokens:
            normalized = raw_token.strip("(){}[];|&")
            if not normalized:
                continue

            if normalized in SHELL_COMMAND_RESETS:
                expect_command = True
                active_probe = ""
                active_wrapper = ""
                wrapper_option_arg_pending = 0
                continue

            if expect_command:
                if self._is_assignment_token(normalized):
                    _, _, value = normalized.partition("=")
                    if COMMAND_SUBSTITUTION_RE.search(value):
                        return True
                    continue
                if normalized in SHELL_KEYWORDS:
                    continue
                if active_wrapper:
                    if wrapper_option_arg_pending:
                        wrapper_option_arg_pending -= 1
                        continue
                    if self._wrapper_consumes_token(active_wrapper, normalized):
                        wrapper_option_arg_pending = self._wrapper_option_arg_count(
                            active_wrapper, normalized
                        )
                        continue
                    if normalized == "python":
                        return True
                    if normalized in SHELL_WRAPPER_COMMANDS:
                        active_wrapper = normalized
                        continue
                    active_wrapper = ""
                    expect_command = False
                    if normalized in SHELL_PROBE_COMMANDS:
                        active_probe = normalized
                    continue
                if normalized in SHELL_WRAPPER_COMMANDS:
                    active_wrapper = normalized
                    continue
                if normalized == "python":
                    return True
                expect_command = False
                if normalized in SHELL_PROBE_COMMANDS:
                    active_probe = normalized
                continue

            if active_probe:
                if normalized.startswith("-"):
                    continue
                return normalized == "python"

        return False

    def check_subprocess_call(self, filepath: Path, line: str, line_num: int) -> bool:
        """Check Python subprocess calls for 'python' usage."""
        # Check for subprocess/os.system/exec calls with 'python'
        subprocess_patterns = [
            r"subprocess\.(run|call|Popen|check_output|check_call)\(\s*\[['\"]python['\"]",
            r"os\.system\(['\"].*\bpython\s",
            r"os\.exec[lv]p?\(['\"]python['\"]",
        ]

        for pattern in subprocess_patterns:
            if re.search(pattern, line) and "python3" not in line:
                self.violations.append(
                    (
                        filepath,
                        line_num,
                        "subprocess",
                        f"Use 'python3' in subprocess call: {line.strip()}",
                    )
                )
                return False
        return True

    def validate_file(self, filepath: Path) -> bool:
        """Validate a single file."""
        try:
            content = filepath.read_text()
        except Exception as e:
            if self.verbose:
                print(f"Error reading {filepath}: {e}")
            return True  # Don't fail on read errors

        lines = content.split("\n")
        violations_found = False

        for line_num, line in enumerate(lines, start=1):
            # Check shebang on first line
            if line_num == 1:
                if not self.check_shebang(filepath, line, line_num):
                    violations_found = True

            # Check shell commands (all file types)
            if not self.check_shell_command(filepath, line, line_num):
                violations_found = True

            # Check Python subprocess calls (Python files only)
            if filepath.suffix == ".py":
                if not self.check_subprocess_call(filepath, line, line_num):
                    violations_found = True

        return not violations_found

    def run(self, files: List[Path]) -> int:
        """Run validator on list of files."""
        if self.verbose:
            print(f"Validating {len(files)} files for python invocation")

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

            for filepath, line_num, violation_type, message in self.violations:
                print(f"  {filepath}:{line_num} ({violation_type})")
                print(f"    {message}\n")

            print(f"Found {len(self.violations)} violation(s)")
            print("\nUsing 'python' instead of 'python3' can cause:")
            print("  - 'command not found' errors on modern systems")
            print("  - Compatibility issues on Ubuntu 20.04+, Debian 11+")
            print("  - CI/CD pipeline failures")
            print("\nFix: Replace 'python' with 'python3' in all commands")

            if not self.strict:
                print("\nThese are warnings. Use --strict to fail on violations.")

            return 1 if self.strict else 0

        if self.verbose:
            print("✓ All files use 'python3' explicitly")

        return 0


# ==============================================================================
# CLI INTERFACE
# ==============================================================================


def main():
    """Main entry point for validator."""
    parser = argparse.ArgumentParser(
        description="Validate python invocation (python3 vs python)",
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

    validator = PythonInvocationValidator(strict=args.strict, verbose=args.verbose)

    exit_code = validator.run(args.files)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
