---
match:
  keywords:
  - implementing optimization without profiling
  - assuming slowness based on complexity
  - building caching before confirming problems
  - this looks complex so it must be slow
  - pytest --profile
  - premature optimization
status: active
---

# Measure Before Optimize

## Rule
Always measure and profile to identify actual performance bottlenecks before implementing optimizations.

## Context
When considering performance improvements or caching implementations for any system component.

## Detection
Observable signals indicating need for measurement:
- Implementing optimization without profiling data
- Assuming slowness based on complexity without measurement
- Building caching solutions before confirming problems exist
- Unable to articulate specific performance metrics that need improvement

## Pattern
Measure first, then decide based on data:
```bash
# Measure performance with profiling
pytest tests/test_lessons*.py --profile --durations=10

# Analyze results to identify actual bottlenecks
# - Check top time consumers
# - Look for operations >100ms
# - Document baseline metrics

# Decision based on data:
# - No bottleneck found? No optimization needed
# - Bottleneck found? Target specific operations
```

**Anti-pattern**: Optimization without measurement
```python
# smell: assumption without data
# "This looks complex, so it must be slow"
# Implements caching for lesson parsing
# No profiling data to support the need
```

## Outcome
Following this pattern leads to:
- **Evidence-based decisions**: Optimize what actually matters
- **Avoid wasted effort**: Don't solve non-existent problems
- **Maintainability**: Simpler code without unnecessary optimization
- **Learning**: Understanding actual performance characteristics

Benefits:
- 5-10 minutes to run profiling vs hours/days implementing premature optimization
- Clear data on actual bottlenecks
- Focus effort where it matters
- Avoid complexity without measured value

## Related
- [Simplify Before Optimize](../patterns/simplify-before-optimize.md) - Simplification principle
- [Requirement Validation](./requirement-validation.md) - Validate before building
- [pytest-profiling documentation](https://pypi.org/project/pytest-profiling/)
