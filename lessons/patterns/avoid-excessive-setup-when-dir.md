---
match:
  keywords:
    - "spending too much time on setup"
    - "5+ chains on prerequisites"
    - "need to understand full pipeline before testing"
    - "excessive setup before testing component"
    - "use mock data instead of full pipeline"
status: active
---

# Avoid Excessive Setup When Testing Components

## Rule
If you spend >5 chains on prerequisites (finding files, understanding pipelines, setting up dependencies), stop and identify a minimal test case. Creating mock data to test one component directly is usually faster than running full pipelines or complex setups.

## Context
This anti-pattern commonly occurs when validating multi-stage pipelines or testing components that depend on outputs from earlier stages. The instinct to "do it properly" by running the full pipeline can lead to excessive setup time that delays actual progress.

## Detection
- You've spent 5+ chains exploring file locations, understanding data flows, or checking for existing outputs
- You catch yourself thinking "I need to understand the full pipeline before I can test this one component"
- You're setting up dependencies for Stage N when you only need to test Stage N+1
- You recognize the thought: "I'm spending a lot of time on setup when I should be making progress"

## Pattern
**When caught in excessive setup:**
1. Stop and count: How many chains have I spent on prerequisites?
2. Ask: "Can I create mock/minimal data to test just the component I care about?"
3. Compare: Full pipeline setup vs. creating mock data + direct test
4. Choose the faster path (usually mocking)
5. Document what mock data represents for future reference

**Example from evidence:**
- Chains 22-38 (16 chains): Exploring Generator→Reflector→Curator pipeline, finding files, checking existing data
- Chain 39: Recognition moment - "spending too much time on validation setup"
- Chains 46-48 (3 chains): Created mock refined insight manually, tested Curator directly
- Result: 3x faster (5 chains total vs 16+ chains)

## Outcome
- **Time savings**: 3x faster completion (5 chains vs 16+ chains in evidence)
- **Faster feedback**: Test the specific component immediately rather than waiting for full pipeline
- **Clearer debugging**: Isolated component testing makes it easier to identify issues
- **Reduced cognitive load**: Focus on one component instead of understanding entire system

## Related
- [Loop Detection](./loop-detection.md) - Related: detect repetitive action loops
