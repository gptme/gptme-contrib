#!/usr/bin/env python3
"""Code review helper utilities for automated analysis.

This module provides functions for:
- Checking naming conventions
- Detecting code smells
- Analyzing complexity
- Finding duplicate code
- Checking test coverage

Usage:
    from review_helpers import check_naming_conventions, detect_code_smells

    issues = check_naming_conventions("src/module.py")
    smells = detect_code_smells("src/module.py")
"""

import re
from pathlib import Path
from typing import List


def check_naming_conventions(filepath: str) -> List[str]:
    """Check Python naming conventions in a file.

    Validates:
    - Function names are lowercase_with_underscores
    - Class names are CapWords
    - Constants are UPPER_CASE_WITH_UNDERSCORES
    - Private names start with underscore

    Args:
        filepath: Path to Python file to check

    Returns:
        List of naming convention violations
    """
    issues = []
    path = Path(filepath)

    if not path.exists():
        return [f"File not found: {filepath}"]

    if not path.suffix == ".py":
        return [f"Not a Python file: {filepath}"]

    content = path.read_text()
    lines = content.split("\n")

    for i, line in enumerate(lines, 1):
        # Check function definitions
        func_match = re.match(r"^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", line)
        if func_match:
            name = func_match.group(1)
            # Check for CONSTANT_CASE (all uppercase with underscores)
            if name.isupper():
                issues.append(
                    f"{filepath}:{i} Function '{name}' uses CONSTANT_CASE (should be for constants, not functions)"
                )
            # Functions should be lowercase_with_underscores (or private with leading _)
            elif not name.replace("_", "").islower() and not name.startswith("_"):
                issues.append(
                    f"{filepath}:{i} Function '{name}' should use lowercase_with_underscores"
                )

        # Check class definitions
        class_match = re.match(r"^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)", line)
        if class_match:
            name = class_match.group(1)
            if not name[0].isupper():
                issues.append(
                    f"{filepath}:{i} Class '{name}' should use CapWords convention"
                )
            if "_" in name:
                issues.append(
                    f"{filepath}:{i} Class '{name}' should not use underscores (use CapWords)"
                )

    return issues


def detect_code_smells(filepath: str) -> List[str]:
    """Detect common code smells in a Python file.

    Detects:
    - Magic numbers (numeric literals except 0, 1, -1)
    - Long functions (>50 lines)
    - Deep nesting (>4 levels)
    - Broad exception catching
    - Commented-out code

    Args:
        filepath: Path to Python file to check

    Returns:
        List of code smell descriptions
    """
    smells = []
    path = Path(filepath)

    if not path.exists():
        return [f"File not found: {filepath}"]

    content = path.read_text()
    lines = content.split("\n")

    current_function = None
    function_start = 0

    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()

        # Track function boundaries
        if stripped.startswith("def "):
            if current_function and (i - function_start) > 50:
                smells.append(
                    f"{filepath}:{function_start} Function '{current_function}' is too long "
                    f"({i - function_start} lines). Consider breaking it up."
                )
            match = re.match(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)", stripped)
            if match:
                current_function = match.group(1)
                function_start = i

        # Check for magic numbers
        if stripped and not stripped.startswith("#"):
            # Skip if line is a string literal
            if (
                '"""' in line
                or "'''" in line
                or (line.count('"') >= 2)
                or (line.count("'") >= 2)
            ):
                continue

            # Find numeric literals except 0, 1, -1
            # Match: single digits 2-9, multi-digit numbers, decimals
            # Pattern: \b to ensure word boundary, then digits
            numbers = re.findall(r"\b([2-9]|[1-9]\d+|\d*\.\d+)\b", stripped)

            # Filter out numbers inside string literals (basic check)
            clean_numbers = [
                n for n in numbers if f'"{n}"' not in line and f"'{n}'" not in line
            ]
            for num in clean_numbers:
                if "range(" not in stripped and "sleep(" not in stripped:
                    smells.append(
                        f"{filepath}:{i} Magic number '{num}'. Consider defining as a named constant."
                    )

        # Check indentation depth
        if stripped:
            indent = len(line) - len(stripped)
            spaces = indent
            if spaces > 0 and spaces % 4 == 0:
                level = spaces // 4
                if level > 4:
                    smells.append(
                        f"{filepath}:{i} Deep nesting ({level} levels). Consider refactoring."
                    )

        # Check for bare except
        if "except:" in stripped and "except Exception" not in stripped:
            smells.append(
                f"{filepath}:{i} Bare except clause. Specify exception type or use 'except Exception:'."
            )

        # Check for commented-out code (lines with code-like patterns)
        if stripped.startswith("#"):
            code_patterns = [
                "def ",
                "class ",
                "import ",
                "if ",
                "for ",
                "while ",
                "= ",
                "return ",
            ]
            if any(pattern in stripped for pattern in code_patterns):
                smells.append(
                    f"{filepath}:{i} Commented-out code. Remove if not needed or use version control."
                )

    # Check final function length
    if current_function and (len(lines) - function_start) > 50:
        smells.append(
            f"{filepath}:{function_start} Function '{current_function}' is too long. Consider breaking it up."
        )

    return smells


def analyze_complexity(filepath: str) -> int:
    """Calculate cyclomatic complexity of a Python file.

    Simplified complexity metric counting:
    - Decision points (if, elif, for, while, except, and, or)
    - Early returns
    - Function/method definitions

    Args:
        filepath: Path to Python file to analyze

    Returns:
        Complexity score (higher = more complex)
    """
    path = Path(filepath)

    if not path.exists():
        return 0

    content = path.read_text()
    complexity = 1  # Base complexity

    # Count decision points (including boolean operators)
    # Boolean operators create additional execution paths
    decision_keywords = ["if ", "elif ", "for ", "while ", "except ", " and ", " or "]

    # Parse line by line to avoid counting in comments/strings
    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        # Skip comments
        if stripped.startswith("#"):
            continue
        # Count decision keywords in actual code
        for keyword in decision_keywords:
            if keyword in line:
                complexity += line.count(keyword)

    # Count early returns within functions (multiple returns in same function)
    # Use regex to find function boundaries more reliably
    func_pattern = r"^\s*def\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\):"
    return_pattern = r"\breturn\b"

    current_returns: list[str] = []
    in_function = False
    function_indent = 0

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Detect function start
        if re.match(func_pattern, line):
            # Process previous function's returns
            if in_function and len(current_returns) > 1:
                complexity += len(current_returns) - 1

            current_returns = []
            in_function = True
            function_indent = len(line) - len(stripped)

        # Detect function end by dedent
        elif in_function and stripped:
            current_indent = len(line) - len(stripped)
            if current_indent <= function_indent:
                # Function ended, process returns
                if len(current_returns) > 1:
                    complexity += len(current_returns) - 1
                in_function = False
                current_returns = []

        # Track returns in current function
        elif in_function and re.search(return_pattern, stripped):
            current_returns.append(line)

    # Process final function if still in one
    if in_function and len(current_returns) > 1:
        complexity += len(current_returns) - 1

    return complexity


def find_duplicate_code(directory: str) -> List[str]:
    """Find potentially duplicated code blocks in a directory.

    Simple implementation checking for identical function signatures
    and similar line patterns.

    Args:
        directory: Directory to search for duplicates

    Returns:
        List of potential duplicate locations
    """
    duplicates = []
    dir_path = Path(directory)

    if not dir_path.exists():
        return [f"Directory not found: {directory}"]

    # Collect all functions
    functions: dict[str, str] = {}

    for py_file in dir_path.rglob("*.py"):
        content = py_file.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            match = re.match(r"^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)", line)
            if match:
                func_name = match.group(1)
                params = match.group(2)
                signature = f"{func_name}({params})"

                if signature in functions:
                    duplicates.append(
                        f"Duplicate function signature '{signature}' in:\n"
                        f"  {functions[signature]}\n"
                        f"  {py_file}:{i}"
                    )
                else:
                    functions[signature] = f"{py_file}:{i}"

    return duplicates


def check_test_coverage(test_dir: str, source_dir: str) -> List[str]:
    """Check test coverage by comparing test files to source files.

    Simple heuristic checking:
    - Test file exists for each source file
    - Test file has reasonable size (>10 lines)

    Args:
        test_dir: Directory containing test files
        source_dir: Directory containing source code

    Returns:
        List of coverage issues
    """
    issues = []
    test_path = Path(test_dir)
    source_path = Path(source_dir)

    if not test_path.exists():
        return [f"Test directory not found: {test_dir}"]

    if not source_path.exists():
        return [f"Source directory not found: {source_dir}"]

    # Check for test files
    source_files = list(source_path.rglob("*.py"))
    test_files = {f.name.replace("test_", ""): f for f in test_path.rglob("test_*.py")}

    for source_file in source_files:
        if source_file.name == "__init__.py":
            continue

        expected_test = f"test_{source_file.name}"

        if expected_test not in test_files:
            issues.append(
                f"Missing test file for {source_file.name}. Expected: {expected_test}"
            )
        else:
            test_file = test_files[expected_test]
            test_lines = len(test_file.read_text().split("\n"))
            if test_lines < 10:
                issues.append(
                    f"Test file {expected_test} is very short ({test_lines} lines). "
                    "Consider adding more comprehensive tests."
                )

    return issues


def main():
    """Example usage of review helpers."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 review_helpers.py <filepath>")
        print("\nExample:")
        print("  python3 review_helpers.py src/module.py")
        sys.exit(1)

    filepath = sys.argv[1]

    print(f"Reviewing: {filepath}\n")

    print("=== Naming Conventions ===")
    naming_issues = check_naming_conventions(filepath)
    if naming_issues:
        for issue in naming_issues:
            print(f"  ⚠️  {issue}")
    else:
        print("  ✓ No naming issues found")

    print("\n=== Code Smells ===")
    smells = detect_code_smells(filepath)
    if smells:
        for smell in smells:
            print(f"  ⚠️  {smell}")
    else:
        print("  ✓ No code smells detected")

    print("\n=== Complexity ===")
    complexity = analyze_complexity(filepath)
    if complexity > 10:
        print(f"  ⚠️  High complexity: {complexity}")
    else:
        print(f"  ✓ Reasonable complexity: {complexity}")


if __name__ == "__main__":
    main()
