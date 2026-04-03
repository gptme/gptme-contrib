---
match:
  keywords:
  - "add tests"
  - "test coverage"
  - "write tests for"
  - "pytest coverage"
  - "missing tests"
status: archived
---

# Code Coverage Principle

## Rule
Pursue code coverage improvements strategically, focusing on bug prevention and refactoring safety, not metric improvement.

## Context
When considering test coverage work during autonomous operation or task selection.

## Detection
Observable signals indicating poor coverage task selection:
- Defaulting to "add tests" when other higher-priority work exists
- Working on coverage just to improve percentage metrics
- Adding tests for trivial code (getters, setters, pass-through functions)
- Coverage work blocking more valuable tasks
- Tests that mirror implementation without catching meaningful bugs

## Pattern
Evaluate coverage work using three key questions:
```text
Question 1: Would this test catch a real bug?
Question 2: Are we refactoring code that needs a safety net?
Question 3: Is there higher-priority work available?

YES to 1 or 2, NO to 3 → Pursue coverage work ✓
NO to 1-2, or YES to 3 → Deprioritize coverage work ✗
```

Real-world decisions:
```text
Bug found in uncovered code → Add tests ✓ (prevents regression)
Refactoring complex logic → Add tests first ✓ (safety net)
Feature needs CI confidence → Add tests ✓ (catch regressions)
Simple getter/setter forgotten → Skip tests ✗ (low value)
"Just improving metrics" → Skip work ✗ (not strategic)
Higher-value issues blocked → Skip coverage ✗ (prioritize blockers)
```

## Outcome
Following this principle enables:
- Strategic use of testing time
- Focus on meaningful coverage (bugs prevented)
- Better task prioritization (higher-value work first)
- Sustainable test maintenance (valuable tests only)

## Related
- [Verifiable Tasks Principle](./verifiable-tasks-principle.md) - Tests enable complex autonomy
