"""Cursor Rules Parser

Parses .cursorrules files and provides conversion to/from gptme lesson format.

Based on research from knowledge/lessons/cursor-rules-format-analysis.md
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re


@dataclass
class CursorRule:
    """Represents a parsed Cursor rule."""

    overview: str
    rules: dict[str, str]  # Section name -> content
    deprecated: str
    file_references: list[str]
    file_patterns: list[str]
    raw_content: str

    @classmethod
    def from_file(cls, path: Path | str) -> "CursorRule":
        """Parse a .cursorrules file."""
        content = Path(path).read_text()
        return cls.from_string(content)

    @classmethod
    def from_string(cls, content: str) -> "CursorRule":
        """Parse Cursor rules from string content."""
        # Extract main sections
        overview = _extract_section(content, "Overview")
        deprecated = _extract_section(content, "Deprecated")

        # Extract rules sections (multiple subsections possible)
        rules = _extract_rules_sections(content)

        # Extract file references (@file syntax)
        file_references = _extract_file_references(content)

        # Extract file patterns (! suffix)
        file_patterns = _extract_file_patterns(content)

        return cls(
            overview=overview,
            rules=rules,
            deprecated=deprecated,
            file_references=file_references,
            file_patterns=file_patterns,
            raw_content=content,
        )

    def to_lesson_format(self) -> dict[str, Any]:
        """Convert Cursor rule to gptme lesson format."""
        # Extract keywords from file patterns and content
        keywords = _generate_keywords_from_patterns(
            self.file_patterns, self.overview, list(self.rules.values())
        )

        # Convert rules sections to lesson content
        lesson_content = self._format_as_lesson()

        return {
            "frontmatter": {
                "match": {
                    "keywords": keywords,
                    "file_patterns": self.file_patterns if self.file_patterns else None,
                }
            },
            "content": lesson_content,
            "source": "cursorrules",
        }

    def _format_as_lesson(self) -> str:
        """Format Cursor rule content as lesson markdown."""
        parts = []

        # Add overview as context
        if self.overview:
            parts.append(f"## Context\n{self.overview}")

        # Add rules as patterns
        for section_name, section_content in self.rules.items():
            parts.append(f"## {section_name}\n{section_content}")

        # Add deprecated patterns as anti-patterns
        if self.deprecated:
            parts.append(f"## Deprecated Patterns\n{self.deprecated}")

        return "\n\n".join(parts)


def _extract_section(content: str, section_name: str) -> str:
    """Extract a top-level section from Cursor rules."""
    # Match: # Section or ## Section
    pattern = rf"^##?\s+{section_name}\s*$"
    match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)

    if not match:
        return ""

    # Find content until next same-level heading
    start = match.end()

    # Find next heading at same or higher level
    next_heading = re.search(r"^##?\s+\w", content[start:], re.MULTILINE)

    if next_heading:
        end = start + next_heading.start()
    else:
        end = len(content)

    return content[start:end].strip()


def _extract_rules_sections(content: str) -> dict[str, str]:
    """Extract all rules subsections."""
    rules: dict[str, str] = {}

    # Find the Rules section
    rules_match = re.search(r"^##?\s+Rules\s*$", content, re.MULTILINE | re.IGNORECASE)
    if not rules_match:
        return rules

    # Get content of Rules section
    start = rules_match.end()

    # Find next top-level heading
    next_section = re.search(r"^##?\s+\w", content[start:], re.MULTILINE)
    rules_content = (
        content[start : start + next_section.start()]
        if next_section
        else content[start:]
    )

    # Extract subsections within Rules
    # Match: ### Subsection or #### Subsection
    subsections = re.finditer(r"^###\s+(.+?)$", rules_content, re.MULTILINE)

    subsection_list = list(subsections)
    for i, match in enumerate(subsection_list):
        section_name = match.group(1).strip()
        section_start = match.end()

        # Find end (next subsection or end of rules)
        if i + 1 < len(subsection_list):
            section_end = subsection_list[i + 1].start()
        else:
            section_end = len(rules_content)

        section_content = rules_content[section_start:section_end].strip()
        rules[section_name] = section_content

    # If no subsections, treat entire Rules section as one entry
    if not rules:
        rules["General"] = rules_content.strip()

    return rules


def _extract_file_references(content: str) -> list[str]:
    """Extract @file references from Cursor rules."""
    # Match: @filename or @path/to/file
    pattern = r"@([\w/.]+)"
    matches = re.findall(pattern, content)
    return list(set(matches))  # Remove duplicates


def _extract_file_patterns(content: str) -> list[str]:
    """Extract file patterns (words ending with !)."""
    # Match: word! or path/pattern!
    pattern = r"([\w/]+)!"
    matches = re.findall(pattern, content)
    return list(set(matches))


def _generate_keywords_from_patterns(
    file_patterns: list[str], overview: str, rules_sections: list[str]
) -> list[str]:
    """Generate keywords from file patterns and content."""
    keywords = set()

    # Keywords from file patterns
    for pattern in file_patterns:
        # Extract technology names from patterns like "tsx!", "python!"
        clean_pattern = pattern.replace("!", "").lower()
        if clean_pattern:
            keywords.add(clean_pattern)

    # Keywords from overview (extract common technical terms)
    overview_lower = overview.lower()
    common_terms = [
        "typescript",
        "react",
        "python",
        "javascript",
        "node",
        "api",
        "test",
        "component",
        "function",
        "class",
        "async",
        "error",
        "style",
        "lint",
        "format",
    ]

    for term in common_terms:
        if term in overview_lower:
            keywords.add(term)

    # Limit to reasonable number
    return sorted(list(keywords))[:10]


def cursor_to_lesson(
    cursor_path: Path | str, output_path: Path | str | None = None
) -> dict[str, Any]:
    """Convert Cursor rules file to gptme lesson format.

    Args:
        cursor_path: Path to .cursorrules file
        output_path: Optional path to save converted lesson

    Returns:
        Dictionary with frontmatter and content for lesson file
    """
    rule = CursorRule.from_file(cursor_path)
    lesson_data = rule.to_lesson_format()

    if output_path:
        _write_lesson_file(lesson_data, output_path)

    return lesson_data


def lesson_to_cursor(
    lesson_path: Path | str, output_path: Path | str | None = None
) -> str:
    """Convert gptme lesson to Cursor rules format.

    Args:
        lesson_path: Path to lesson file
        output_path: Optional path to save converted Cursor rules

    Returns:
        Cursor rules content as string
    """
    # Read lesson file
    content = Path(lesson_path).read_text()

    # Parse frontmatter and content
    parts = content.split("---", 2)
    if len(parts) >= 3:
        lesson_content = parts[2].strip()
    else:
        lesson_content = content.strip()

    # Convert to Cursor format (simplified - can be enhanced)
    cursor_content = _format_as_cursorrules(lesson_content)

    if output_path:
        Path(output_path).write_text(cursor_content)

    return cursor_content


def _write_lesson_file(lesson_data: dict[str, Any], output_path: Path | str) -> None:
    """Write lesson data to file with YAML frontmatter."""
    import yaml

    frontmatter = yaml.dump(lesson_data["frontmatter"], default_flow_style=False)
    content = lesson_data["content"]

    full_content = f"---\n{frontmatter}---\n\n{content}"

    Path(output_path).write_text(full_content)


def _format_as_cursorrules(lesson_content: str) -> str:
    """Format lesson content as Cursor rules."""
    # Demote all headings by one level (# → ##, ## → ###, etc.)
    # This properly nests lesson sections under "## Rules"
    demoted_content = re.sub(r"^(#{1,5}) ", r"#\1 ", lesson_content, flags=re.MULTILINE)

    lines = []
    lines.append("# Overview")
    lines.append("This rule was converted from a gptme lesson.")
    lines.append("")
    lines.append("## Rules")
    lines.append("")
    lines.append(demoted_content)

    return "\n".join(lines)


# CLI interface
def main() -> None:
    """CLI interface for Cursor rules parser."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: cursorrules_parser.py <command> [args]")
        print("")
        print("Commands:")
        print("  parse <file>           - Parse and display Cursor rules")
        print("  to-lesson <file>       - Convert Cursor rules to lesson")
        print("  from-lesson <file>     - Convert lesson to Cursor rules")
        sys.exit(1)

    command = sys.argv[1]

    if command == "parse":
        if len(sys.argv) < 3:
            print("Usage: cursorrules_parser.py parse <file>")
            sys.exit(1)

        rule = CursorRule.from_file(sys.argv[2])
        print(f"Overview: {rule.overview[:100]}...")
        print(f"Rules sections: {list(rule.rules.keys())}")
        print(f"File references: {rule.file_references}")
        print(f"File patterns: {rule.file_patterns}")

    elif command == "to-lesson":
        if len(sys.argv) < 3:
            print("Usage: cursorrules_parser.py to-lesson <cursor-file> [output-file]")
            sys.exit(1)

        cursor_file = sys.argv[2]
        output_file = sys.argv[3] if len(sys.argv) > 3 else None

        lesson_data = cursor_to_lesson(cursor_file, output_file)

        if output_file:
            print(f"Converted {cursor_file} -> {output_file}")
        else:
            print("Lesson data:")
            print(lesson_data)

    elif command == "from-lesson":
        if len(sys.argv) < 3:
            print(
                "Usage: cursorrules_parser.py from-lesson <lesson-file> [output-file]"
            )
            sys.exit(1)

        lesson_file = sys.argv[2]
        output_file = sys.argv[3] if len(sys.argv) > 3 else None

        cursor_content = lesson_to_cursor(lesson_file, output_file)

        if output_file:
            print(f"Converted {lesson_file} -> {output_file}")
        else:
            print("Cursor rules content:")
            print(cursor_content)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
