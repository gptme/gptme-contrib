---
match:
  keywords:
    - "file content getting cut off mid-codeblock"
    - "files ending with incomplete content"
    - "codeblock without language tag causing parse errors"
    - "save operation truncating content"
    - "unclosed code block"
    - "code block that's not closed"
    - "there's no closing"
automation:
  status: automated
  validator: scripts/precommit/validators/validate_markdown_codeblock_syntax.py
  enforcement: warning
  automated_date: 2025-11-26
status: active
---

# Markdown Codeblock Syntax

## Rule
Always specify language tags for markdown codeblocks (```txt, ```csv, ```ascii, ```diagram) to prevent parsing cut-offs.

## Context
When writing markdown files with code blocks, especially in save/append operations for journals, documentation, or data files.

## Detection
Observable signals that indicate this rule is needed:
- Writing codeblocks without language tags
- Files ending with "# Header line" or "Title:"
- Content getting cut off mid-codeblock
- Having to append "the rest" after incomplete saves

## Pattern
Always specify appropriate language tag:
````markdown
# ✅ Correct: Explicit language tags
```txt
Plain text content
Multiple lines preserved
```

```csv
header1,header2,header3
value1,value2,value3
```

```ascii
┌─────────┐
│ Diagram │
└─────────┘
```

```diagram
flowchart TD
    A --> B
```
````

# ❌ Wrong: No language tag

````markdown
```
Content here
Gets cut off
````


## Outcome
Following this pattern prevents:
- **Token waste**: 123+ recovery attempts avoided (~12.7% of sessions)
- **Data loss**: Files cut off mid-content
- **Follow-up work**: Extra tool calls to "append the rest"
- **Parsing errors**: Ambiguous codeblock boundaries

Benefits:
- Clean file saves without cut-offs
- Parser correctly identifies content boundaries
- No need for recovery attempts
- Complete content preserved first time

**What happens without language tags**: Parser may misinterpret closing ```, cutting content early. Your attention should recognize this pattern when seeing incomplete saves or files ending with ":" or "**".

## Related
- Full context: https://github.com/ErikBjare/bob/blob/master/knowledge/lessons/tools/markdown-codeblock-syntax.md
