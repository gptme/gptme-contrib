# Skills Directory

This directory contains **skills** - enhanced lessons that bundle workflows, scripts, and utilities for gptme.

## Overview

Skills extend gptme's lesson system by providing executable components alongside instructional content. While lessons teach patterns and best practices, skills provide ready-to-use workflows with supporting tools.

### Skills vs. Lessons

| Feature | Lesson | Skill |
|---------|--------|-------|
| **Purpose** | Behavioral guidance | Executable workflows |
| **Content** | Patterns, rules, examples | Instructions + bundled scripts |
| **Activation** | Automatic via keywords | Explicit loading |
| **Length** | 30-50 lines (primary) | Hundreds of lines |
| **Scripts** | None | Bundled helper utilities |
| **Dependencies** | None | Python packages if needed |

### When to Use

**Use Lessons when:**
- Teaching cross-cutting patterns
- Enforcing constraints
- Providing behavioral guidance
- Need automatic activation via keywords

**Use Skills when:**
- Providing complete workflows
- Bundling helper scripts
- Including templates or resources
- Need explicit tool selection

## Available Skills

### 1. template-skill

**Purpose**: Minimal skill template for creating new skills

**Use cases**: Starting point for creating your own skills

**Features**:
- Basic structure demonstration
- YAML frontmatter examples
- Documentation patterns

**Keywords**: `skill template`, `create skill`, `new skill`

### 2. code-review-helper

**Purpose**: Systematic code review workflows with automation utilities

**Use cases**:
- Reviewing pull requests
- Conducting code audits
- Providing structured feedback

**Features**:
- Systematic review process (6 dimensions)
- Bundled Python utilities for analysis
- Structured feedback format
- Integration with GitHub CLI

**Keywords**: `code review`, `pr review`, `review code`, `code quality`, `code audit`

**Bundled utilities** (`review_helpers.py`):
- `check_naming_conventions()` - PEP 8 validation
- `detect_code_smells()` - Common anti-pattern detection
- `analyze_complexity()` - Cyclomatic complexity
- `find_duplicate_code()` - Duplicate detection
- `check_test_coverage()` - Test file analysis

## Using Skills

### Loading a Skill

Skills are explicitly loaded when needed:

```text
> User: I need to do a code review
> Assistant: Let me load the code-review-helper skill...

[Skill loaded explicitly by assistant]
```

### Accessing Bundled Scripts

Skills include helper scripts that can be imported or executed:

```python
# Import from skill's bundled utilities
from code_review_helper.review_helpers import check_naming_conventions

# Analyze a file
issues = check_naming_conventions("src/module.py")
for issue in issues:
    print(issue)
```

```shell
# Run bundled script directly
python3 skills/code-review-helper/review_helpers.py src/module.py
```

## Creating New Skills

### 1. Directory Structure

Create a new directory in `skills/`:

```text
skills/your-skill-name/
├── SKILL.md              # Required: Main documentation
└── helper_script.py      # Optional: Bundled utilities
```

### 2. SKILL.md Format

Create `SKILL.md` with YAML frontmatter following Anthropic's skill format:

```yaml
---
name: skill-name
description: Brief one-line description of what the skill does and when to use it
---
```

**Note**: Skills use Anthropic's minimal frontmatter format with only `name` and `description`. For dependencies, create a `requirements.txt` file in the skill directory.

### 3. Documentation Structure

Structure your SKILL.md with clear sections:

```markdown
# Skill Name

## Overview
What this skill does and when to use it

## Workflow
Step-by-step process with examples

## Bundled Utilities
Documentation for any helper scripts

## Examples
Concrete usage examples

## Related
Links to lessons, other skills, or resources
```

### 4. Bundled Scripts

If including Python utilities:

1. **Self-contained**: All code in one file when possible
2. **Documented**: Docstrings for all public functions
3. **Executable**: Include `if __name__ == "__main__"` block
4. **Dependencies**: List in YAML frontmatter

Example structure:
```python
#!/usr/bin/env python3
"""Brief module description.

Usage:
    from skill_name.helper import function_name
    result = function_name(args)
"""

def helper_function(arg: str) -> str:
    """Function documentation."""
    return result

def main():
    """CLI usage example."""
    pass

if __name__ == "__main__":
    main()
```

## Best Practices

### Skill Design

- **Focused scope**: One workflow or tool per skill
- **Clear naming**: Descriptive directory names (use hyphens)
- **Minimal dependencies**: Only essential packages
- **Complete examples**: Working code samples in docs

### Documentation

- **Self-contained**: User should understand from SKILL.md alone
- **Practical examples**: Show real usage, not toy examples
- **Link to lessons**: Connect to related behavioral patterns
- **Version tracking**: Update version on significant changes

### Maintenance

- **Status field**: Mark beta/deprecated as appropriate
- **Test utilities**: Verify bundled scripts work
- **Update docs**: Keep examples synchronized with code
- **Dependencies**: Keep dependency versions current

## Migration from Lessons

When converting a lesson to a skill:

1. **Preserve lesson**: Keep the original lesson for keyword matching
2. **Create skill**: Build complete workflow with utilities
3. **Link bidirectionally**: Lesson references skill, skill references lesson
4. **Clear distinction**: Lesson = when/why, Skill = what/how

Example:
- Lesson: `lessons/workflow/code-review-best-practices.md` (30 lines)
- Skill: `skills/code-review-helper/SKILL.md` (200+ lines + utilities)
- Lesson references skill for complete workflow
- Skill references lesson for patterns and constraints
