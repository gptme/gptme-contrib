# Lessons Package

Lesson validation, analysis, and management system for persistent learning patterns.

## Overview

This package provides tools for managing the lessons system - a meta-learning framework that captures behavioral patterns, prevents known failures, and improves agent reliability over time.

## Features

- **Lesson Validation**: Validate lesson format and frontmatter
- **Lesson Analysis**: Analyze lesson effectiveness and usage patterns
- **Lesson Generation**: Tools for creating new lessons from patterns
- **Workflow Management**: Lesson lifecycle management (creation, review, adoption)
- **Format Conversion**: Convert lessons to/from various formats (Markdown, YAML, Cursor rules)

## Installation

```bash
# From workspace root
uv sync --all-packages

# Or install just this package
cd packages/lessons
uv sync
```

## Dependencies

- **PyYAML**: YAML parsing for lesson frontmatter
- **click**: CLI interface for lesson tools

## Usage

### Validation

```python
from lessons.validate import validate_lesson_file

# Validate single lesson
errors = validate_lesson_file("path/to/lesson.md")
if errors:
    print(f"Validation errors: {errors}")
```

### CLI Tools

```bash
# Validate lessons
./validate.py lessons/workflow/example.md

# Analyze lesson usage
./analytics.py

# Generate new lesson
./generate.py --pattern "error pattern"

# Review lessons
./review.py
```

## Lesson Format

Lessons use YAML frontmatter + Markdown:

```markdown
---
match:
  keywords: ["keyword1", "keyword2"]
status: active
---

# Lesson Title

## Rule
One-sentence imperative: what to do or avoid.

## Context
When this applies.

## Detection
Observable signals.

## Pattern
Correct approach with example.

## Outcome
What happens when you follow this pattern.

## Related
- Related lessons
```

## Testing

```bash
# Run all tests
make test

# Type check
make typecheck
```

## Architecture

### Core Modules

- **validate.py**: Lesson format validation
- **analytics.py**: Usage and effectiveness analysis
- **generate.py**: New lesson creation tools
- **review.py**: Lesson review and refinement
- **workflow.py**: Lesson lifecycle management
- **adopt.py**: Lesson adoption and integration

### Validation Rules

The validator checks:
- Valid YAML frontmatter
- Required sections present
- Proper Markdown formatting
- Keyword specificity
- Status values

## Integration

### Pre-commit Hooks

Lessons are validated automatically:

```yaml
- id: validate-lessons
  name: Validate lesson files
  entry: ./scripts/lessons/validate.py
  language: system
  files: ^lessons/.*\.md$
```

### gptme Integration

Lessons are automatically included in gptme context when relevant keywords match.

## Related Documentation

- See the [gptme lessons documentation](https://gptme.org/docs/lessons.html) for lesson system overview
- Check the lessons/ directory in gptme-agent-template for example lessons

## Development

### Adding New Tools

1. Create tool script in `src/lessons/`
2. Use click for CLI interface
3. Follow existing patterns for validation/analysis
4. Add tests
5. Document in this README
