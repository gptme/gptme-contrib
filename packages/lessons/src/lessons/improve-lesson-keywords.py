#!/usr/bin/env python3
"""
Script to improve lesson keywords based on Issue #37 analysis.

This script helps identify and fix overly general keywords that cause false positives.
"""

import re
from pathlib import Path
from typing import Dict, List, Tuple


def extract_keywords(lesson_path: Path) -> Tuple[List[str], str]:
    """Extract keywords and full frontmatter from lesson file."""
    with open(lesson_path) as f:
        content = f.read()

    # Extract frontmatter keywords
    match = re.search(r"match:\s*\n\s*keywords:\s*\[(.*?)\]", content, re.DOTALL)
    if not match:
        return [], ""

    keywords_str = match.group(1)
    keywords = [k.strip().strip('"').strip("'") for k in keywords_str.split(",")]

    return keywords, content


def suggest_improvements() -> Dict[str, List[str]]:
    """Suggest keyword improvements for problematic lessons."""

    improvements = {
        "shell-variable-syntax.md": [
            "Replace: shell → shell variable",
            "Replace: variable → bare variable, dollar sign",
            "Keep: command not found (specific error)",
            "Keep: bash (specific context)",
            "Suggested: [shell variable, bare variable, dollar sign, command not found, bash]",
        ],
        "shell-path-quoting.md": [
            "Replace: shell → remove (too generic)",
            "Replace: path → path spaces, quoted path",
            "Keep: spaces, quoting, cd, too many arguments (specific)",
            "Suggested: [path spaces, quoted path, spaces quoting, cd, too many arguments]",
        ],
        "shell-command-chaining.md": [
            "Replace: shell → remove (too generic)",
            "Replace: command → command chaining",
            "Keep: pipe, chaining, sequential (specific)",
            "Suggested: [command chaining, pipe, sequential, operator]",
        ],
        "shell-command-reconstruction.md": [
            "Replace: shell → shell reconstruction",
            "Replace: command → command reconstruction",
            "Keep: pipe, operators, shlex, expansion, compound (specific)",
            "Suggested: [shell reconstruction, command reconstruction, pipe, operators, shlex, tilde expansion, compound]",
        ],
        "directory-structure-awareness.md": [
            "Replace: directory → project directory, project location",
            "Replace: path → remove (too generic with 'cd' already)",
            "Keep: cd, projects, Programming (specific to issue)",
            "Suggested: [project directory, project location, cd, projects, Programming, /home/bob]",
        ],
        "working-directory-awareness.md": [
            "Replace: directory → working directory, cwd",
            "Replace: path → remove (covered by 'working directory')",
            "Keep: script, no such file, relative path, pwd (specific)",
            "Suggested: [working directory, cwd, script, no such file, relative path, pwd]",
        ],
        "absolute-paths-for-workspace-files.md": [
            "Replace: path → absolute path, relative path",
            "Replace: directory → remove (covered by context)",
            "Keep: journal, append, save, workspace (specific)",
            "Suggested: [absolute path, relative path, journal, append, save, workspace]",
        ],
        "python-invocation.md": [
            "Replace: python → python command, python3 missing",
            "Keep: python3, command not found, invocation (specific)",
            "Suggested: [python command, python3, python3 missing, command not found, invocation]",
        ],
        "python-file-execution.md": [
            "Replace: python → python file, python script",
            "Keep: permission denied, execute, interpreter (specific)",
            "Suggested: [python file, python script, permission denied, execute, interpreter, shebang]",
        ],
        "verify-tool-availability.md": [
            "Replace: command → command not found (more specific)",
            "Keep: not found, tool, dependency (specific)",
            "Add: missing tool",
            "Suggested: [command not found, missing tool, tool availability, dependency, pytest, npm]",
        ],
    }

    return improvements


def analyze_keyword_specificity() -> None:
    """Analyze and report keyword specificity issues."""

    lessons_dir = Path("lessons")

    print("=" * 80)
    print("LESSON KEYWORD SPECIFICITY ANALYSIS")
    print("=" * 80)
    print()

    # Generic keywords to flag
    generic_keywords = {
        "shell",
        "command",
        "path",
        "directory",
        "python",
        "test",
        "ci",
        "build",
        "debug",
        "verification",
    }

    print("🔍 Lessons with overly generic keywords:\n")

    for lesson_file in sorted(lessons_dir.rglob("*.md")):
        keywords, _ = extract_keywords(lesson_file)
        if not keywords:
            continue

        generic_found = [k for k in keywords if k in generic_keywords]
        if generic_found:
            print(f"📄 {lesson_file.name}")
            print(f"   Generic: {', '.join(generic_found)}")
            print(f"   All: {', '.join(keywords)}")
            print()

    print("\n" + "=" * 80)
    print("SUGGESTED IMPROVEMENTS")
    print("=" * 80)
    print()

    improvements = suggest_improvements()
    for lesson_name, suggestions in improvements.items():
        print(f"📄 {lesson_name}")
        for suggestion in suggestions:
            print(f"   • {suggestion}")
        print()

    print("=" * 80)
    print("NEXT STEPS")
    print("=" * 80)
    print()
    print("1. Review suggestions above")
    print("2. Update lesson frontmatter manually")
    print("3. Test keyword matching on sample conversations")
    print("4. Iterate based on precision metrics")
    print()
    print("For automatic updates, implement apply_improvements() function.")


def apply_improvements() -> None:
    """
    Apply keyword improvements automatically (STUB - implement as needed).

    This would:
    1. Read lesson file
    2. Update frontmatter keywords
    3. Write back to file
    4. Commit changes
    """
    print("Automatic application not yet implemented.")
    print("Please update lessons manually based on suggestions above.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--apply":
        apply_improvements()
    else:
        analyze_keyword_specificity()
