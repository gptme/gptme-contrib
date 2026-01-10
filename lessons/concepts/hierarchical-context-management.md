---
match:
  keywords:
  - hierarchical context management
  - Droid context architecture
  - agent context layers
  - immediate context
  - session context
  - project context
  - global context
  - context cascading
  - Factory Droid
  - context layer architecture
---

# Hierarchical Context Management (Droid Architecture)

## Term
**Hierarchical Context Management**: Context architecture used in Factory's Droid and similar advanced agent systems.

## Definition
Multi-layered context management approach that organizes information in cascading levels of importance and accessibility, assembling context per-step based on the current action.

## Architecture Layers

Per Factory's Droid documentation:

- **Global Context**: System policies, guardrails, organization conventions, role definitions
- **Project/Repo Context**: Structural view of codebase, framework conventions, key entrypoints (retrieved on-demand via tools)
- **Session/Task Context**: Original request, evolving task state, decomposition artifacts from plan mode
- **Plan/Subtask Context**: Structured plan with steps, dependencies, required files - only active step injected
- **Action-Level Context**: Concrete files, test outputs, logs, diffs needed for next edit/command + local scratchpad
- **Tool/Environment Layer**: Tools via MCP, file system, git, test runner - results selectively injected

## Key Principles

1. **Step-wise assembly**: Context built per-step based on current action (planning, editing, testing, etc.)
2. **Aggressive pruning**: Summarize older reasoning, limit file content to relevant slices, drop completed branches
3. **Role-aware prompts**: Each specialized agent has own context configuration
4. **Guardrails by default**: Constraints in global and role layers enforced via prompt + tool policy

## Disambiguation

**Note**: This is DIFFERENT from the "CASCADE" workflow used in gptme autonomous sessions, which refers to the task selection priority order: PRIMARY → SECONDARY → TERTIARY. That CASCADE is about task prioritization, not context management architecture.

## Source
Factory AI's Droid product and public documentation on agentic context engineering.

## Related
- ACE (Agentic Context Engineering)
- Context compression strategies
- Memory hierarchy patterns
- Multi-agent architectures
