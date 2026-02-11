---
match:
  keywords:
  - deep nesting 4+ levels
  - nested if statement pyramid
  - guard clause pattern
  - early return for simplicity
  - function extraction for nesting
  - arrow code smell
  - too many indentation levels
status: active
---

# Avoid Deeply Nested Code Structures

## Rule
Refactor code when nesting exceeds 3-4 levels using guard clauses, function extraction, or condition inversion.

## Context
When writing or refactoring code, particularly during patch operations or code generation where deep nesting causes maintenance burden.

## Detection
Observable signals that refactoring is needed:
- Code nesting exceeds 3-4 levels deep
- Difficulty understanding logic flow
- Patch tool struggles with complex indentation
- Function exceeds ~50 lines with nested structure
- Multiple nested if/for/while statements

## Pattern
Flatten structure using early returns and function extraction:

```python
# Before: Deep nesting (6 levels)
def process_data(items):
    if items:
        for item in items:
            if item.is_valid():
                if item.needs_processing():
                    result = item.process()
                    if result.success:
                        return result
    return None

# After: Flattened (2 levels max)
def process_data(items):
    if not items:
        return None

    for item in items:
        result = process_single_item(item)
        if result:
            return result

    return None

def process_single_item(item):
    if not item.is_valid():
        return None
    if not item.needs_processing():
        return None

    result = item.process()
    return result if result.success else None
```

## Outcome
Following this pattern leads to:
- **Readability**: Logic flow is clear and linear
- **Maintainability**: Easy to modify and extend
- **Patch reliability**: Simple indentation for patch tool
- **Testability**: Isolated functions are easier to test

## Related
- [Avoid Long Try Blocks](./avoid-long-try-blocks.md) - Related code structure pattern
