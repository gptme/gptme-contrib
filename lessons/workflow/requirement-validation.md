---
match:
  keywords:
  - WHO requested this feature
  - WHAT problem does this solve
  - validate before implementing
  - seems like a good idea without requester
  - building before validating need
status: active
---

# Requirement Validation Before Implementation

## Rule
Before implementing any feature, validate: WHO requested it, WHAT problem it solves, HOW to measure success, and WHAT'S the minimal solution.

## Context
Applies when starting any new work (feature, PR, task, automation).

## Detection
Observable signals:
- Starting implementation without clear requester
- Building comprehensive solution before validating need
- "Seems like a good idea" without specific problem statement
- Feature scope growing during development

## Pattern
BEFORE implementation:
```yaml
requirement_validation:
  who_requested: "User" | "Reviewer feedback" | "Specific issue"
  problem_statement: "Clear statement of actual problem"
  success_criteria: "How we'll know it works"
  minimal_solution: "Simplest version that solves problem"
  validation_method: "Test before building full version"
```

EXAMPLE - Good validation:
```yaml
who_requested: "Code review feedback"
problem: "Documentation links broken (404 errors)"
success: "Links resolve correctly"
minimal: "Fix 3 broken links"
validation: "Click links, verify they work"
```

EXAMPLE - Missing validation:
```yaml
who_requested: "Seemed necessary" # ❌
problem: "Thought we needed it" # ❌
success: "Unclear" # ❌
minimal: "6000+ line system" # ❌
validation: "Build first, validate later" # ❌
```

## Outcome
Following this prevents:
- Over-engineering (building before validating)
- Feature creep (unclear requirements)
- Wasted effort (solving wrong problem)
- Abandoned PRs (reviewer: "not sure it's even good/useful")

## Related
- [Simplify Before Optimize](../patterns/simplify-before-optimize.md) - Reduction principle
- Elon's Step 1: "Make requirements less dumb"
