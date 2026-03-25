---
match:
  keywords:
  - "how do I know when this is done"
  - "no way to verify"
  - "unclear completion criteria"
  - "task success is unclear"
status: active
---

# Verifiable Tasks Principle

## Rule
Prioritize complex tasks with strong verification mechanisms over simpler tasks without objective feedback.

## Context
When selecting autonomous work, especially for complex or high-value tasks.

## Detection
Observable signals indicating poor task selection:
- Avoiding complex tasks because "too hard for autonomous"
- Selecting simple tasks just because they seem "safe"
- Working on subjective tasks without clear completion criteria
- Spending time on work where success/failure is ambiguous

## Pattern
Choose based on verifiability, not complexity:
```text
HIGH Verifiability (Choose):
  - Implement feature with test suite
  - Fix bug with failing tests
  - Refactor with type checking + tests
  - Performance optimization with benchmarks

LOW Verifiability (Deprioritize):
  - Creative writing (subjective)
  - Design decisions (judgment)
  - Strategic planning (needs input)
```

Key insight: Tests + CI + Type checking = Objective verification → Autonomy-friendly

## Outcome
Following this principle enables:
- Autonomous work on complex problems (with verification)
- Quality output through iterative improvement
- Confidence in results (tests pass = done)
- Higher value delivery (complex > simple)

## Related
- [Code Coverage Principle](./code-coverage-principle.md) - Tests enable complex autonomy
