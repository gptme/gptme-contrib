---
match:
  keywords:
  - context management
  - CASCADE
  - hierarchy
  - agent systems
  - context architecture
  - immediate context
  - session context
  - project context
  - global context
  - information organization
  - context levels
---

# CASCADE (Context Management Strategy)

## Term
**CASCADE**: Context management architecture used in advanced agent systems

## Definition
Hierarchical context management approach that organizes information in cascading levels of importance and accessibility

## Implementation
- **Immediate Context**: Current task and recent decisions (always loaded)
- **Session Context**: Current conversation and related work (conditionally loaded)
- **Project Context**: Long-term project memory and patterns (retrieved on-demand)
- **Global Context**: Cross-project knowledge and lessons (indexed access)

## Purpose
- Prevents context overflow while maintaining access to relevant information
- Balances immediate needs with broader knowledge access
- Optimizes token usage through hierarchical loading

## Context
Used in Factory's Droid and similar advanced agent architectures for intelligent context management beyond simple compression.

## Related
- Context compression
- Memory hierarchy
- Information architecture
- Agent memory systems
