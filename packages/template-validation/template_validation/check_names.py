"""Name pattern validation for template and fork checking."""
import re
from pathlib import Path
from typing import List, Dict, Optional


# Default exclusions for fork mode
FORK_MODE_EXCLUDES = [
    "docs/",
    "knowledge/",
    "journal/",
    "*.md",
    "lessons/",
    "skills/",
]

# Template patterns (check in template-mode)
TEMPLATE_PATTERNS = {
    "gptme-agent-incomplete": r"gptme-agent(?!-template)",
    "agent-name-placeholder": r"agent-name",
    "agent-name-bracket": r"\[AGENT_NAME\]",
    "your-name-bracket": r"\[YOUR_NAME\]",
}

# Fork patterns (check in fork-mode, code files only)
FORK_PATTERNS = {
    "template-reference": r"gptme-agent-template",
    "template-suffix": r"-template(?!/)",
}


class ValidationResult:
    """Result of validation check."""
    
    def __init__(self):
        self.violations: List[Dict[str, str]] = []
        self.files_checked: int = 0
        
    def add_violation(self, file_path: str, pattern: str, line: str, line_num: int):
        """Add a validation violation."""
        self.violations.append({
            "file": file_path,
            "pattern": pattern,
            "line": line.strip(),
            "line_num": line_num,
        })
        
    def is_valid(self) -> bool:
        """Check if validation passed."""
        return len(self.violations) == 0
    
    def format_report(self) -> str:
        """Format violations as human-readable report."""
        if self.is_valid():
            return f"✓ Validation passed ({self.files_checked} files checked)"
        
        report = [f"✗ Found {len(self.violations)} violations in {len(set(v['file'] for v in self.violations))} files:\n"]
        
        # Group by file
        by_file = {}
        for v in self.violations:
            if v['file'] not in by_file:
                by_file[v['file']] = []
            by_file[v['file']].append(v)
            
        for file_path, violations in by_file.items():
            report.append(f"\n{file_path}:")
            for v in violations:
                report.append(f"  Line {v['line_num']}: {v['pattern']}")
                report.append(f"    {v['line']}")
                
        return "\n".join(report)


def should_exclude(file_path: Path, excludes: List[str]) -> bool:
    """Check if file should be excluded from checking."""
    path_str = str(file_path)
    
    for pattern in excludes:
        # Handle directory patterns
        if pattern.endswith("/"):
            if any(part == pattern.rstrip("/") for part in file_path.parts):
                return True
        # Handle glob patterns
        elif "*" in pattern:
            if file_path.match(pattern):
                return True
        # Handle exact matches
        elif pattern in path_str:
            return True
            
    return False


def check_file(
    file_path: Path,
    patterns: Dict[str, str],
    root: Path,
) -> List[Dict[str, str]]:
    """Check a single file for pattern violations."""
    violations = []
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                for pattern_name, pattern in patterns.items():
                    if re.search(pattern, line, re.IGNORECASE):
                        violations.append({
                            "file": str(file_path.relative_to(root)),
                            "pattern": pattern_name,
                            "line": line.strip(),
                            "line_num": line_num,
                        })
    except Exception as e:
        # Skip files that can't be read
        pass
        
    return violations


def validate_names(
    root: Path,
    mode: str = "fork",
    excludes: Optional[List[str]] = None,
    custom_patterns: Optional[Dict[str, str]] = None,
) -> ValidationResult:
    """
    Validate naming patterns in repository.
    
    Args:
        root: Root directory to check
        mode: "template" or "fork"
        excludes: List of patterns to exclude
        custom_patterns: Additional patterns to check
        
    Returns:
        ValidationResult with violations
    """
    result = ValidationResult()
    
    # Determine patterns and exclusions
    if mode == "template":
        patterns = TEMPLATE_PATTERNS.copy()
        default_excludes = []
    else:  # fork mode
        patterns = FORK_PATTERNS.copy()
        default_excludes = FORK_MODE_EXCLUDES.copy()
        
    # Add custom patterns
    if custom_patterns:
        patterns.update(custom_patterns)
        
    # Merge excludes
    exclude_list = default_excludes + (excludes or [])
    
    # Walk directory tree
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
            
        if should_exclude(file_path, exclude_list):
            continue
            
        result.files_checked += 1
        
        violations = check_file(file_path, patterns, root)
        for v in violations:
            result.add_violation(v["file"], v["pattern"], v["line"], v["line_num"])
            
    return result


def validate_agent_identity(root: Path) -> List[str]:
    """
    Validate agent has proper identity configuration.
    
    Returns list of validation errors.
    """
    errors = []
    
    # Check gptme.toml
    gptme_toml = root / "gptme.toml"
    if not gptme_toml.exists():
        errors.append("Missing gptme.toml file")
    else:
        content = gptme_toml.read_text()
        if "[agent]" not in content:
            errors.append("gptme.toml missing [agent] section")
        elif 'name = "Agent"' in content or 'name = "agent-name"' in content:
            errors.append("gptme.toml has default agent name")
            
    # Check ABOUT.md
    about_md = root / "ABOUT.md"
    if not about_md.exists():
        errors.append("Missing ABOUT.md file")
    else:
        content = about_md.read_text()
        placeholders = ["[AGENT_NAME]", "[YOUR_NAME]", "agent-name"]
        for placeholder in placeholders:
            if placeholder in content:
                errors.append(f"ABOUT.md contains placeholder: {placeholder}")
                break
                
    return errors
