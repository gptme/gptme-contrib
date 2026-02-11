---
match:
  keywords: ["self-review", "pre-landing", "review iteration", "code review", "substantial change"]
status: active
---

# Pre-Landing Self-Review

## Rule
Before committing changes >100 lines, touching >5 files, or modifying core infrastructure, run through the self-review checklist.

## Context
After implementing significant changes but before committing/completing the session.

## Detection
Observable signals that self-review is needed:
- Changed >100 lines of code
- Modified core infrastructure (executor, storage, main workflows)
- High-priority (P0) issue work
- Introduced new patterns or abstractions
- Uncertain about edge cases or design choices
- Created new public APIs or interfaces

Quick check:
```shell
git diff --stat main | tail -5  # How much changed?
git diff --name-only main | grep -E "(core|main|critical)"  # Core files?
```

## Pattern
Self-review iteration questions:

```text
1. EDGE CASES
   - [ ] Null/empty inputs handled?
   - [ ] Boundary conditions tested?
   - [ ] Concurrent access considered?

2. ERROR PATHS
   - [ ] All exceptions caught appropriately?
   - [ ] Error messages helpful?
   - [ ] Failure modes graceful?

3. TEST COVERAGE
   - [ ] Critical paths tested?
   - [ ] Edge cases have tests?
   - [ ] Tests actually run and pass?

4. DOCUMENTATION
   - [ ] Docstrings/comments accurate?
   - [ ] README updated if needed?
   - [ ] Changes documented in journal?

5. CODE QUALITY
   - [ ] Naming clear and consistent?
   - [ ] No obvious code smells?
   - [ ] Follows existing patterns?
```

## Skip Self-Review If
- Trivial change (≤50 lines, ≤3 files)
- Pure documentation updates
- Test-only changes
- Simple bug fix with clear, limited scope
- Refactoring with no logic changes

## Outcome
Following this pattern prevents:
- Shipping edge case bugs
- Incomplete error handling
- Missing test coverage
- Stale documentation

Benefits:
- Catch issues before CI
- Higher quality first-time commits
- Reduced review cycles
- Professional discipline

## Related
- [Session Ending Protocol](./session-ending-protocol.md) - Full session checklist
- Source: Adapted from steveyegge/vc Pre-Landing Review Protocol
