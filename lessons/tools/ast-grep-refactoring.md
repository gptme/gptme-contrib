---
match:
  keywords:
  - sg run --pattern
  - ast-grep structural search
  - code refactoring across files
  - replace pattern in codebase
  - AST-based code transformation
status: active
---

# Using ast-grep for Code Refactoring

## Rule
Use ast-grep (sg) for structural code search and refactoring when patterns are complex or language-specific.

## Context
When you need to find or refactor code patterns across many files, enforce coding standards, or perform precise structural searches beyond regex capabilities.

## Detection
Observable signals you should use ast-grep:
- Using sed/awk for complex code transformations (brittle, error-prone)
- Manual find-and-replace missing edge cases
- Regex patterns that don't respect code structure
- Text-based refactoring breaking code unexpectedly
- Need to handle language-specific syntax correctly

## Pattern
Structural search and rewrite with ast-grep:
```shell
# Search for function patterns
sg run --pattern 'def $FUNC($ARGS): $$$' --lang python src/

# Rewrite with preview
sg run --pattern 'print($MSG)' --rewrite 'logger.info($MSG)' --lang python

# Interactive selective changes
sg run --pattern 'old_func($ARGS)' --rewrite 'new_func($ARGS)' --lang python -i
```

**Anti-pattern**: Text-based refactoring
```shell
# smell: brittle text manipulation
sed -i 's/old_func/new_func/g' *.py  # breaks strings, comments
grep -r "def.*function" --include="*.py"  # imprecise
```

## Outcome
Following this pattern leads to:
- Accurate structural refactoring (AST-based, not text)
- Safe changes (preview before applying)
- Language-aware handling (respects syntax)
- Fast execution (even on large codebases)
- No false matches in comments or strings

## Related
- [ast-grep Playground](https://ast-grep.github.io/playground.html) - Test patterns
- [Pattern Syntax Guide](https://ast-grep.github.io/guide/pattern-syntax.html)
