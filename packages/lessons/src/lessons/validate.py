#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "pyyaml",
# ]
# ///
"""
Validation script for lesson files.

Checks lesson quality and structure against requirements.
Supports two formats:
1. Original format: Full lesson with all sections (pre-Issue #45)
2. Two-file format: Concise primary + companion doc (Issue #45)

Combines functionality from:
- scripts/learn/validate.py (section validation, format detection)
- scripts/lesson-checker.py (length checks, companion doc validation)
"""

import sys

import re
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


# Configuration
TARGET_LENGTH = 100  # lines (soft target for primary lessons)
COMPANION_DIR = Path("knowledge/lessons")


class LessonValidator:
    """Validates lesson structure and content.

    Supports two lesson formats:
    1. Original format: Full lesson with all sections in one file
    2. Two-file format: Concise primary lesson + companion documentation

    Attributes:
        ORIGINAL_REQUIRED_SECTIONS: List of required sections for original format
        TWO_FILE_REQUIRED_SECTIONS: List of required sections for two-file format
        MIN_FAILURE_SIGNALS: Minimum number of failure signals required
        MIN_VERIFICATION_ITEMS: Minimum number of verification checklist items
        MIN_AUTOMATION_HOOKS: Minimum number of automation hooks
        filepath: Path to lesson file being validated
        content: Content of lesson file
        errors: List of validation errors
        warnings: List of validation warnings
        format_type: Detected lesson format ('original' or 'two-file')
    """

    # Original format (verbose, all-in-one)
    ORIGINAL_REQUIRED_SECTIONS = [
        "Rule",
        "Context",
        "Failure Signals",
        "Anti-pattern (concise)",
        "Recommended Pattern",
        "Fix Recipe",
        "Rationale",
        "Verification Checklist",
        "Exceptions",
        "Automation Hooks",
        "Origin",
        "Related",
    ]

    # Two-file format (concise primary lesson)
    TWO_FILE_REQUIRED_SECTIONS = [
        "Rule",
        "Context",
        "Detection",
        "Pattern",
        "Outcome",
        "Related",
    ]

    MIN_FAILURE_SIGNALS = 2
    MIN_VERIFICATION_ITEMS = 1
    MIN_AUTOMATION_HOOKS = 1

    def __init__(self, filepath: Path):
        """Initialize validator for a lesson file.

        Args:
            filepath: Path to lesson file to validate
        """
        self.filepath = filepath
        self.content = filepath.read_text()
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.format_type: Optional[str] = None

    def _detect_format(self) -> str:
        """Detect which format this lesson uses.

        Returns:
            'two-file' if lesson uses concise format with companion doc,
            'original' if using full all-in-one format
        """
        # Check for two-file format indicators
        has_detection = bool(
            re.search(
                r"^##\s+Detection\s*$", self.content, re.MULTILINE | re.IGNORECASE
            )
        )
        has_pattern = bool(
            re.search(r"^##\s+Pattern\s*$", self.content, re.MULTILINE | re.IGNORECASE)
        )
        has_outcome = bool(
            re.search(r"^##\s+Outcome\s*$", self.content, re.MULTILINE | re.IGNORECASE)
        )

        # Check for companion doc link in Related section
        has_companion_link = bool(
            re.search(r"knowledge/lessons/.*\.md", self.content, re.IGNORECASE)
        )

        # If has Detection, Pattern, and Outcome (or companion link), it's two-file format
        if (has_detection and has_pattern and has_outcome) or has_companion_link:
            return "two-file"

        # Otherwise assume original format
        return "original"

    def validate(self) -> bool:
        """Run all validation checks on the lesson file.

        Returns:
            True if lesson passes all validation checks, False otherwise
        """
        self._check_frontmatter()

        # Detect format
        self.format_type = self._detect_format()

        # Validate based on format
        if self.format_type == "two-file":
            self._validate_two_file_format()
        else:
            self._validate_original_format()

        return len(self.errors) == 0

    def _check_frontmatter(self):
        """Check for required YAML frontmatter and validate structure.

        Ensures frontmatter exists, is valid YAML, and contains only allowed fields.
        """
        if not self.content.startswith("---"):
            self.errors.append("Missing YAML frontmatter")
            return

        try:
            # Extract frontmatter
            end_idx = self.content.index("---", 3)
            frontmatter_text = self.content[3:end_idx]
            frontmatter = yaml.safe_load(frontmatter_text)

            if frontmatter is None:
                self.errors.append("Empty frontmatter")
                return

            # Check for minimal frontmatter (should primarily be 'match' with keywords)
            allowed_fields = {
                "match",
                "status",
                "automated_by",
                "automated_date",
                "deprecated_by",
                "deprecated_date",
                "archived_reason",
                "archived_date",
            }
            extra_fields = set(frontmatter.keys()) - allowed_fields
            if extra_fields:
                self.warnings.append(
                    f"Frontmatter should be minimal. Consider removing: {', '.join(extra_fields)}"
                )

        except (ValueError, yaml.YAMLError) as e:
            self.errors.append(f"Invalid YAML frontmatter: {e}")

    def _validate_two_file_format(self):
        """Validate two-file format (concise primary + companion)."""
        # Check required sections
        self._check_required_sections(self.TWO_FILE_REQUIRED_SECTIONS)

        # Check Detection section has meaningful content
        self._check_detection_content()

        # Check for companion doc
        self._check_companion_doc()

        # Check length (soft warning)
        self._check_length()

        # Check for verbose sections that belong in companion
        self._check_verbose_sections()

    def _validate_original_format(self):
        """Validate original format (complete lesson in one file)."""
        # Check required sections
        self._check_required_sections(self.ORIGINAL_REQUIRED_SECTIONS)

        # Additional quality checks for original format
        self._check_failure_signals()
        self._check_verification_checklist()
        self._check_automation_hooks()

    def _check_required_sections(self, required_sections: List[str]):
        """Check for presence of required sections."""
        for section in required_sections:
            # Escape special regex characters in section name
            section_pattern = re.escape(section)
            pattern = rf"^##\s+{section_pattern}\s*$"

            if not re.search(pattern, self.content, re.MULTILINE | re.IGNORECASE):
                self.errors.append(f"Missing required section: {section}")

    def _check_detection_content(self):
        """Check Detection section has meaningful content."""
        detection_match = re.search(
            r"^##\s+Detection\s*$(.+?)(?=^##|\Z)",
            self.content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )

        if detection_match:
            detection_content = detection_match.group(1).strip()
            # Should have multiple detection signals (list items or paragraphs)
            if len(detection_content) < 50:  # Minimum meaningful content
                self.warnings.append(
                    "Detection section seems too brief. Should describe observable signals."
                )

    def _check_companion_doc(self):
        """Check for companion doc existence and linking."""
        # Check if companion doc exists
        companion_path = COMPANION_DIR / f"{self.filepath.stem}.md"
        has_companion = companion_path.exists()

        # Check if linked in Related section
        has_companion_link = bool(
            re.search(
                rf"knowledge/lessons/{self.filepath.stem}\.md",
                self.content,
                re.IGNORECASE,
            )
        )

        # Get line count
        lines = self.content.split("\n")
        frontmatter_end = self.content.index("---", 3) if "---" in self.content else 0
        body_start = len(self.content[:frontmatter_end].split("\n"))
        body_lines = len(lines) - body_start

        # If lesson is long but no companion, suggest creating one
        if body_lines > TARGET_LENGTH and not has_companion:
            self.warnings.append(
                f"Primary lesson is {body_lines} lines (target: {TARGET_LENGTH}). "
                f"Consider creating companion: knowledge/lessons/{self.filepath.stem}.md"
            )

        # If companion exists but not linked, warn
        if has_companion and not has_companion_link:
            self.warnings.append(
                f"Companion doc exists but not linked. Add to Related section: "
                f"knowledge/lessons/{self.filepath.stem}.md"
            )

    def _check_length(self):
        """Check primary lesson length."""
        lines = self.content.split("\n")

        # Calculate body lines (exclude frontmatter)
        if self.content.startswith("---"):
            try:
                end_idx = self.content.index("---", 3)
                body_start = len(self.content[:end_idx].split("\n"))
                body_lines = len(lines) - body_start
            except ValueError:
                body_lines = len(lines)
        else:
            body_lines = len(lines)

        # Soft target warning
        if body_lines > TARGET_LENGTH:
            self.warnings.append(
                f"Primary lesson is {body_lines} lines (target: {TARGET_LENGTH}). "
                "Consider if more content could move to companion."
            )

    def _check_verbose_sections(self):
        """Check for sections that belong in companion doc."""
        verbose_sections = [
            "Verification Checklist",
            "Automation Hooks",
            "Origin",
            "Exceptions",
            "Common Pitfalls",
            "Implementation",
            "Rationale",
        ]

        found_verbose = []
        for section in verbose_sections:
            pattern = rf"^##\s+{re.escape(section)}\s*$"
            if re.search(pattern, self.content, re.MULTILINE | re.IGNORECASE):
                found_verbose.append(section)

        if found_verbose:
            companion_path = COMPANION_DIR / f"{self.filepath.stem}.md"
            if not companion_path.exists():
                self.warnings.append(
                    f"Contains sections better suited for companion doc: {', '.join(found_verbose)}. "
                    f"Consider creating: knowledge/lessons/{self.filepath.stem}.md"
                )

    def _check_failure_signals(self):
        """Check for minimum number of failure signals (original format)."""
        # Find Failure Signals section
        signals_match = re.search(
            r"^##\s+Failure Signals\s*$(.+?)(?=^##|\Z)",
            self.content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )

        if not signals_match:
            return  # Already reported as missing section

        signals_content = signals_match.group(1)

        # Count list items (lines starting with -, *, or numbers with .)
        # Matches: "- item", "* item", or "1. item", "1) item"
        signals = re.findall(
            r"^\s*(?:[-*]|\d+[.)])\s+.+$", signals_content, re.MULTILINE
        )

        if len(signals) < self.MIN_FAILURE_SIGNALS:
            self.errors.append(
                f"Insufficient failure signals: found {len(signals)}, "
                f"need at least {self.MIN_FAILURE_SIGNALS}"
            )

    def _check_verification_checklist(self):
        """Check for minimum number of verification items (original format)."""
        # Find Verification Checklist section
        checklist_match = re.search(
            r"^##\s+Verification Checklist\s*$(.+?)(?=^##|\Z)",
            self.content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )

        if not checklist_match:
            return  # Already reported as missing section

        checklist_content = checklist_match.group(1)

        # Count checkbox items (lines with - [ ])
        checklist_items = re.findall(
            r"^\s*[-*]\s+\[[ x]\]\s+.+$", checklist_content, re.MULTILINE
        )

        if len(checklist_items) < self.MIN_VERIFICATION_ITEMS:
            self.errors.append(
                f"Insufficient verification items: found {len(checklist_items)}, "
                f"need at least {self.MIN_VERIFICATION_ITEMS}"
            )

    def _check_automation_hooks(self):
        """Check for minimum number of automation hooks (original format)."""
        # Find Automation Hooks section
        hooks_match = re.search(
            r"^##\s+Automation Hooks\s*$(.+?)(?=^##|\Z)",
            self.content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )

        if not hooks_match:
            return  # Already reported as missing section

        hooks_content = hooks_match.group(1)

        # Count list items or mentions of automation tools
        automation_patterns = [
            r"pre-commit",
            r"CI",
            r"lint",
            r"grep",
            r"pattern",
            r"script",
            r"check",
        ]

        found_hooks = False
        for pattern in automation_patterns:
            if re.search(pattern, hooks_content, re.IGNORECASE):
                found_hooks = True
                break

        if not found_hooks:
            self.errors.append(
                f"No automation hooks found. Include at least {self.MIN_AUTOMATION_HOOKS} "
                "automation mechanism (pre-commit, CI, lint, grep pattern, etc.)"
            )

    def print_results(self, verbose: bool = False):
        """Print validation results."""
        format_label = "two-file" if self.format_type == "two-file" else "original"

        if self.errors:
            print(
                f"❌ Validation failed for {self.filepath.name} ({format_label} format)\n"
            )
            print("Errors:")
            for error in self.errors:
                print(f"  - {error}")

        if self.warnings and (verbose or not self.errors):
            if not self.errors:
                print(f"⚠️  Warnings for {self.filepath.name} ({format_label} format)")
            else:
                print("\nWarnings:")
            for warning in self.warnings:
                print(f"  - {warning}")

        if not self.errors and not self.warnings:
            print(f"✅ {self.filepath.name} is valid ({format_label} format)")


def validate_lesson_file(filepath: Path, verbose: bool = False) -> bool:
    """Validate a single lesson file. Returns True if valid."""
    validator = LessonValidator(filepath)
    valid = validator.validate()
    validator.print_results(verbose=verbose)
    return valid


def validate_directory(
    directory: Path, recursive: bool = True, verbose: bool = False
) -> Tuple[int, int]:
    """
    Validate all lesson files in a directory.
    Returns (valid_count, total_count).
    """
    pattern = "**/*.md" if recursive else "*.md"
    lesson_files = [
        f
        for f in directory.glob(pattern)
        if f.is_file()
        and not f.name.startswith("README")
        # Skip symlinks pointing to gptme-contrib (validated separately)
        and not (f.is_symlink() and "gptme-contrib" in str(f.resolve()))
    ]

    if not lesson_files:
        print(f"No lesson files found in {directory}")
        return 0, 0

    valid_count = 0
    total_count = len(lesson_files)

    print(f"Validating {total_count} lesson files in {directory}...\n")

    for filepath in lesson_files:
        if validate_lesson_file(filepath, verbose=verbose):
            valid_count += 1
        print()  # Blank line between files

    # Summary
    print("=" * 50)
    print(f"Summary: {valid_count}/{total_count} lessons valid")
    print("=" * 50)

    return valid_count, total_count


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate lesson files")
    parser.add_argument(
        "paths",
        type=Path,
        nargs="+",
        help="Path(s) to lesson file(s) or directory(ies)",
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        default=True,
        help="Recursively validate directories (default: True)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show warnings and additional details",
    )

    args = parser.parse_args()

    # Track overall success across all paths
    all_valid = True

    for path in args.paths:
        if not path.exists():
            print(f"Error: Path not found: {path}")
            all_valid = False
            continue

        if path.is_file():
            # Validate single file
            valid = validate_lesson_file(path, verbose=args.verbose)
            if not valid:
                all_valid = False
        else:
            # Validate directory
            valid_count, total_count = validate_directory(
                path, recursive=args.recursive, verbose=args.verbose
            )
            if valid_count != total_count:
                all_valid = False

    sys.exit(0 if all_valid else 1)


if __name__ == "__main__":
    main()
