# Cursor Rules Parser

Parser and converter for Cursor AI editor's `.cursorrules` format, enabling bidirectional conversion with gptme's lesson system.

## Purpose

This module provides cross-system compatibility between:
- **Cursor rules**: Project-specific coding standards for Cursor AI editor
- **gptme lessons**: Behavioral guidance and failure prevention patterns

Based on research: `knowledge/lessons/cursor-rules-format-analysis.md`

## Features

- **Parse Cursor rules**: Extract sections, file references, and patterns
- **Convert to lessons**: Generate gptme lesson format with keywords
- **Convert from lessons**: Export lessons as Cursor rules
- **CLI interface**: Command-line tools for all operations

## Usage

### Parsing Cursor Rules

```bash
# Parse and display structure
python3 cursorrules_parser.py parse <cursorrules-file>

# Example output:
# Overview: This project uses TypeScript...
# Rules sections: ['Always', 'Auto Attached: tsx!']
# File references: ['README.md', 'docs/architecture.md']
# File patterns: ['tsx', 'test']
```

### Converting to Lesson Format

```bash
# Convert Cursor rules to gptme lesson
python3 cursorrules_parser.py to-lesson <cursorrules-file> [output-file]

# Example:
python3 cursorrules_parser.py to-lesson examples/example.cursorrules examples/converted.md
```

Output includes:
- YAML frontmatter with keywords and file patterns
- Converted sections (Context, Rules, Deprecated Patterns)
- Proper lesson structure

### Converting from Lesson Format

```bash
# Convert gptme lesson to Cursor rules
python3 cursorrules_parser.py from-lesson <lesson-file> [output-file]

# Example:
python3 cursorrules_parser.py from-lesson ../../lessons/tools/shell-workflow.md output.cursorrules
```

### Python API

```python
from cursorrules_parser import CursorRule, cursor_to_lesson, lesson_to_cursor

# Parse Cursor rules
rule = CursorRule.from_file("project/.cursorrules")
print(rule.overview)
print(rule.rules)
print(rule.file_patterns)

# Convert to lesson
lesson_data = cursor_to_lesson("project/.cursorrules", "output/lesson.md")

# Convert from lesson
cursor_content = lesson_to_cursor("lessons/my-lesson.md", "output/.cursorrules")
```

## Cursor Rules Format

### Structure

```markdown
# Overview
High-level project description and context

## Rules

### Always
Rules that always apply

### Auto Attached: pattern!
Rules auto-attached when file matches pattern

### Agent Requested
Rules applied only when requested

## Deprecated
Patterns to avoid (anti-patterns)

## References
- @file.md - File references
```

### Special Syntax

- **File references**: `@filename.md` - Reference specific files
- **File patterns**: `pattern!` - Auto-attach rules to matching files
- **Subsections**: Organize rules by type or trigger condition

## Lesson Format Output

Converted lessons include:

```yaml
---
match:
  keywords: [typescript, react, test]  # Auto-generated from content
  file_patterns: [tsx, test]          # Extracted from rules
---

## Context
[Overview section content]

## [Rule Section Name]
[Rule section content]

## Deprecated Patterns
[Deprecated section content]
```

## Implementation Details

### Keyword Generation

Keywords are automatically extracted from:
1. **File patterns**: `tsx!` → keyword: `tsx`
2. **Overview content**: Technical terms (typescript, react, api, etc.)
3. **Common terms**: Limited to top 10 most relevant

### Section Parsing

The parser handles:
- Multiple heading levels (`#` and `##` for top-level, `###` for subsections)
- Flexible section naming (case-insensitive matching)
- Content until next same-level heading
- Empty sections (returns empty string)

### File References

Extracts two types:
- **Explicit**: `@filename.md` → Direct file references
- **Patterns**: `pattern!` → File pattern triggers

## Examples

See `examples/` directory:
- `example.cursorrules` - TypeScript/React project rules
- `converted-lesson.md` - Converted lesson format

## Testing

```bash
# Test parsing
python3 cursorrules_parser.py parse examples/example.cursorrules

# Test conversion
python3 cursorrules_parser.py to-lesson examples/example.cursorrules test-output.md
python3 cursorrules_parser.py from-lesson test-output.md test-output.cursorrules

# Verify output
cat test-output.md
cat test-output.cursorrules
```

## Integration with gptme

### Current Status (Phase 5.1)

✅ Parser implemented
✅ Bidirectional conversion working
✅ CLI interface functional
⏳ gptme integration (Phase 5.2)
⏳ CLI commands in gptme (Phase 5.3)

### Future Integration (Phase 5.2-5.3)

Phase 5.2:
- Auto-detect `.cursorrules` in project roots
- Load and convert to internal lesson format
- Respect file patterns for auto-attachment

Phase 5.3:
- Add `/cursor show` command
- Add `/cursor convert` command
- Add `/lesson export --cursor` command

## Design Decisions

### Why Bidirectional Conversion?

Cursor rules and gptme lessons serve **complementary purposes**:
- **Cursor rules**: Prescriptive, project-specific coding standards
- **gptme lessons**: Descriptive, behavioral guidance across projects

Bidirectional conversion enables:
- Sharing patterns between systems
- Project-specific overrides of general lessons
- Cross-system compatibility

### Why Not Folder Structure?

Cursor rules use single-file format (`.cursorrules`), not folders. We preserve this convention to:
- Maintain compatibility with Cursor editor
- Follow established community patterns
- Keep project roots clean

## References

- **Research**: `knowledge/lessons/cursor-rules-format-analysis.md`
- **Claude Skills Analysis**: `knowledge/lessons/claude-skills-analysis.md`
- **Implementation Plan**: `knowledge/technical-designs/lesson-system-phase4-6-plan.md`

## Related

- [gptme Lesson System](../../lessons/README.md)
- [Skills System](../skills/README.md)
- [Cursor Documentation](https://cursor.com/docs/context/rules)
