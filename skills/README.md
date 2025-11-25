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

Skills are activated by keywords in conversation or can be referenced directly:

```text
> User: I need to do a code review
> Assistant: I'll use the code-review-helper skill...

[Skill loaded automatically via keywords]
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
