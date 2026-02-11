---
match:
  keywords:
    - "adding new code without refactoring"
    - "proposing refactoring without doing it"
    - "shared module created but not used"
    - "duplicated code after adding abstraction"
    - "be bold in refactoring"
status: active
---

# Bold Refactoring

## Rule
When creating shared modules or abstractions, immediately refactor existing code to use them. Don't just add - reduce.

## Context
When adding new code that could be shared, or when creating abstractions that should replace existing implementations.

## Detection
Observable signals that bold refactoring is needed:
- Created a shared module but existing code still has duplicate logic
- PR diff shows only additions (+1,660 −0) when refactoring opportunity exists
- Proposed using a module "later" without doing it now
- Said "the server CAN now use" instead of "the server NOW uses"

## Pattern
When adding shared code, immediately apply it:

```text
# ❌ WRONG: Add shared module, propose future use
1. Create shared workspace module
2. CLI uses new module ✓
3. Server still has duplicated code
4. Comment: "Server CAN import from workspace"
5. PR: +1,660 -0 (all additions)

# ✅ CORRECT: Add AND refactor in same PR
1. Create shared workspace module
2. CLI uses new module ✓
3. Refactor server to use module ✓
4. PR: +1,693 -132 (net addition but with refactoring)
```

**Before refactoring** (duplicated logic inline):
```python
# 200+ lines of duplicated setup, cloning, config merging...
subprocess.run(["git", "clone", ...])
subprocess.run(["git", "submodule", ...])
# ... same logic repeated in multiple places
```

**After refactoring** (uses shared module):
```python
from .shared import create_workspace, init_project
# ~100 lines using shared functions
```

## Outcome
Following this pattern:
- **Code quality**: Less duplication, single source of truth
- **Maintainability**: Changes in one place affect all callers
- **Professional PRs**: Show refactoring discipline, not just accumulation
- **Reviewer confidence**: Demonstrates understanding of codebase

Anti-pattern symptoms:
- Growing codebase without consolidation
- "I'll refactor later" (you won't)
- PRs that only add, never remove

## Related
- [Simplify Before Optimize](../patterns/simplify-before-optimize.md) - Reduction principle
